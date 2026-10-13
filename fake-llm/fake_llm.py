"""Fake OpenAI-compatible LLM server for the Sonde demo.

Standard library only. It simulates what matters for observability:
realistic latency per model, token usage (with prompt caching), tool-calling
plans, agents that wander in loops on vague prompts, hallucinated tool names,
rate limits and server errors.

Faults can be changed at runtime, which is handy during a live demo:

    curl -s localhost:8080/admin/faults
    curl -s -X POST localhost:8080/admin/faults -d '{"rate_limit_rate": 0.4}'
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

MODEL_PROFILES = {
    # base latency (ms), generation speed (ms per output token), prefill (ms per input token)
    "sim-large": {"base_ms": 450, "ms_per_token": 9.0, "prefill_ms_per_token": 0.05},
    "sim-mini": {"base_ms": 180, "ms_per_token": 4.0, "prefill_ms_per_token": 0.02},
}
DEFAULT_PROFILE = {"base_ms": 300, "ms_per_token": 6.0, "prefill_ms_per_token": 0.03}

SERVICES = ["checkout-service", "payments-api", "orders-api", "auth-service", "search-api", "kafka"]

# Tool-calling plan per task category: one entry per model turn.
PLANS = {
    "latency": [["get_service_metrics"], ["query_logs"], ["search_runbooks"]],
    "errors": [["query_logs"], ["get_service_metrics"], ["search_runbooks"], ["create_incident_ticket"]],
    "capacity": [["get_service_metrics"], ["search_runbooks"]],
    "security": [["query_logs"], ["block_ip_address"], ["search_runbooks"], ["create_incident_ticket"]],
}
WANDER_TOOLS = ["search_runbooks", "query_logs", "get_service_metrics"]

FINAL_ANSWERS = {
    "latency": (
        "Root cause: {svc} p95 latency rose after the last deploy; slow queries on the "
        "inventory database dominate the traces. Recommended action: roll back the deploy "
        "or enable the read replica (runbook RB-102)."
    ),
    "errors": (
        "Root cause: {svc} returns 5xx when its upstream connection pool is exhausted. "
        "An incident ticket was opened. Recommended action: raise the pool size and add a "
        "circuit breaker (runbook RB-204)."
    ),
    "capacity": (
        "{svc} is under-provisioned for the current load. Recommended action: scale the "
        "consumer group and review partition count (runbook RB-311)."
    ),
    "security": (
        "The 401 spike on {svc} comes from a credential-stuffing pattern on a small set of "
        "IPs. An incident ticket was opened for the security team (runbook RB-512)."
    ),
    "vague": (
        "I could not find a clear anomaly. Could you share which service, metric or time window you are worried about?"
    ),
}


class Faults:
    """Runtime-tunable behaviour. Every value is a probability unless noted."""

    _lock = threading.Lock()
    values: ClassVar[dict[str, float]] = {
        "rate_limit_rate": float(os.getenv("FAKE_LLM_RATE_LIMIT_RATE", "0.04")),
        "error_rate": float(os.getenv("FAKE_LLM_ERROR_RATE", "0.01")),
        "loop_rate": float(os.getenv("FAKE_LLM_LOOP_RATE", "0.75")),
        "hallucinated_tool_rate": float(os.getenv("FAKE_LLM_HALLUCINATED_TOOL_RATE", "0.5")),
        "parallel_tool_rate": float(os.getenv("FAKE_LLM_PARALLEL_TOOL_RATE", "0.3")),
        # small models second-guess themselves: extra tool round instead of answering
        "small_model_dither_rate": float(os.getenv("FAKE_LLM_SMALL_MODEL_DITHER_RATE", "0.25")),
        "latency_factor": float(os.getenv("FAKE_LLM_LATENCY_FACTOR", "1.0")),  # multiplier
        "retry_after_s": float(os.getenv("FAKE_LLM_RETRY_AFTER_S", "1")),  # seconds
    }

    @classmethod
    def get(cls, key: str) -> float:
        with cls._lock:
            return cls.values[key]

    @classmethod
    def snapshot(cls) -> dict:
        with cls._lock:
            return dict(cls.values)

    @classmethod
    def update(cls, changes: dict) -> dict:
        with cls._lock:
            for key, value in changes.items():
                if key not in cls.values:
                    raise KeyError(key)
                cls.values[key] = float(value)
            return dict(cls.values)


def count_tokens(text: str) -> int:
    """Rough tokenizer: ~4 characters per token."""
    return max(1, math.ceil(len(text) / 4))


def classify(task: str) -> str:
    text = task.lower()
    if re.search(r"\b401\b|attack|brute|suspicious|credential", text):
        return "security"
    if re.search(r"latenc|slow|p95|p99", text):
        return "latency"
    if re.search(r"\b5\d\d\b|error|exception|fail", text):
        return "errors"
    if re.search(r"\blag\b|disk|capacity|memory|cpu|saturat", text):
        return "capacity"
    return "vague"


def find_service(task: str) -> str:
    for service in SERVICES:
        if service in task:
            return service
    return "unknown-service"


def tool_arguments(name: str, service: str, category: str, rng: random.Random) -> dict:
    if name == "get_service_metrics":
        return {"service": service, "window_minutes": rng.choice([15, 30, 60])}
    if name == "query_logs":
        return {"service": service, "level": "error", "limit": 20}
    if name == "search_runbooks":
        return {"query": f"{service} {category}"}
    if name == "create_incident_ticket":
        return {"title": f"[{service}] {category} investigation", "severity": "SEV2"}
    if name == "block_ip_address":
        return {"ip": "203.0.113.42", "duration_minutes": 60}
    return {}


class Conversation:
    """What the fake model can infer from the request history."""

    def __init__(self, messages: list[dict]):
        self.messages = messages
        user_indexes = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        last_user = user_indexes[-1] if user_indexes else 0
        self.task = str(messages[last_user].get("content") or "") if messages else ""
        self.category = classify(self.task)
        self.service = find_service(self.task)
        self.tool_rounds = sum(
            1 for m in messages[last_user + 1 :] if m.get("role") == "assistant" and m.get("tool_calls")
        )
        self.has_history = any(m.get("role") == "assistant" for m in messages)
        self.system_prompt = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")


def decide_tools(conv: Conversation, model: str, rng: random.Random) -> list[str]:
    """Return the tool names to call this turn, or [] to answer."""
    if conv.category == "vague":
        if conv.tool_rounds < 2 or rng.random() < Faults.get("loop_rate"):
            return [rng.choice(WANDER_TOOLS)]
        return []

    plan = PLANS[conv.category]
    if conv.tool_rounds >= len(plan):
        if "mini" in model and rng.random() < Faults.get("small_model_dither_rate"):
            return ["search_runbooks"]
        return []
    step = list(plan[conv.tool_rounds])
    if step == ["block_ip_address"] and rng.random() >= Faults.get("hallucinated_tool_rate"):
        step = ["get_service_metrics"]
    if conv.tool_rounds == 0 and conv.category == "latency" and rng.random() < Faults.get("parallel_tool_rate"):
        step = ["get_service_metrics", "query_logs"]
    return step


def complete(body: dict, rng: random.Random) -> dict:
    model = str(body.get("model") or "sim-mini")
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    conv = Conversation(messages)

    tool_names = decide_tools(conv, model, rng)
    if tool_names:
        tool_calls = [
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(tool_arguments(name, conv.service, conv.category, rng)),
                },
            }
            for name in tool_names
        ]
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        finish_reason = "tool_calls"
        generated = json.dumps(tool_calls)
    else:
        text = FINAL_ANSWERS[conv.category].format(svc=conv.service)
        message = {"role": "assistant", "content": text}
        finish_reason = "stop"
        generated = text

    prompt_tokens = count_tokens(json.dumps(messages) + json.dumps(tools))
    # Prompt caching: the static prefix (system prompt + tool definitions) is
    # served from cache once the conversation already has a model turn.
    cached_tokens = 0
    if conv.has_history:
        cached_tokens = min(prompt_tokens, count_tokens(conv.system_prompt + json.dumps(tools)))
    # Hidden reasoning tokens make output usage noisier than the visible text.
    completion_tokens = count_tokens(generated) + rng.randint(15, 120)

    profile = MODEL_PROFILES.get(model, DEFAULT_PROFILE)
    latency_ms = (
        profile["base_ms"]
        + profile["ms_per_token"] * completion_tokens
        + profile["prefill_ms_per_token"] * (prompt_tokens - cached_tokens)
    ) * rng.lognormvariate(0, 0.35)
    time.sleep(latency_ms / 1000 * Faults.get("latency_factor"))

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"{model}-2026-06-01" if model in MODEL_PROFILES else model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-llm/1.0"
    rng = random.Random()

    def log_message(self, fmt, *args):  # keep container logs quiet
        if os.getenv("FAKE_LLM_ACCESS_LOG") == "1":
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/v1/models":
            models = [{"id": name, "object": "model", "owned_by": "sonde"} for name in MODEL_PROFILES]
            self._send(200, {"object": "list", "data": models})
        elif self.path == "/admin/faults":
            self._send(200, Faults.snapshot())
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid JSON", "type": "invalid_request_error"}})
            return

        if self.path == "/admin/faults":
            try:
                self._send(200, Faults.update(body))
            except (KeyError, TypeError, ValueError) as exc:
                self._send(400, {"error": {"message": f"unknown or invalid fault: {exc}"}})
            return

        if self.path != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found"}})
            return

        if self.rng.random() < Faults.get("rate_limit_rate"):
            self._send(
                429,
                {"error": {"message": "Rate limit reached", "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
                {"Retry-After": f"{Faults.get('retry_after_s'):g}"},
            )
            return
        if self.rng.random() < Faults.get("error_rate"):
            self._send(503, {"error": {"message": "The server is overloaded", "type": "server_error"}})
            return

        self._send(200, complete(body, self.rng))


def make_server(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    port = int(os.getenv("FAKE_LLM_PORT", "8080"))
    server = make_server(port=port)
    print(f"fake-llm listening on :{port} faults={Faults.snapshot()}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
