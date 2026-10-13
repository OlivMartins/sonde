"""Minimal OpenAI-compatible chat client with GenAI tracing.

Works with any OpenAI-compatible endpoint: the bundled fake LLM, OpenAI,
Ollama, vLLM, Mistral, LiteLLM...
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import semconv as sc

tracer = trace.get_tracer("sonde_agent.llm")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __iadd__(self, other: Usage) -> Usage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        return self


@dataclass
class ChatResult:
    message: dict
    finish_reason: str
    usage: Usage
    tool_calls: list[dict] = field(default_factory=list)


def to_semconv_messages(messages: list[dict]) -> list[dict]:
    """Convert OpenAI chat messages to the GenAI semconv message format.

    The system prompt is skipped on purpose: it is versioned with the agent
    (gen_ai.agent.version), so storing it on every span would only cost money.
    """
    converted = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        parts: list[dict] = []
        if role == "tool":
            parts.append(
                {"type": "tool_call_response", "id": message.get("tool_call_id"), "response": message.get("content")}
            )
        else:
            if message.get("content"):
                parts.append({"type": "text", "content": message["content"]})
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = function.get("arguments")
                parts.append(
                    {"type": "tool_call", "id": call.get("id"), "name": function.get("name"), "arguments": arguments}
                )
        converted.append({"role": role, "parts": parts})
    return converted


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        provider: str = "openai",
        capture_content: bool = False,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        timeout_s: float = 60.0,
    ):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s)
        parsed = urlparse(base_url)
        self._server_address = parsed.hostname or ""
        self._server_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.provider = provider
        self.capture_content = capture_content
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s

    def close(self) -> None:
        self._http.close()

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        temperature: float,
        max_tokens: int,
        step: int,
    ) -> ChatResult:
        with tracer.start_as_current_span(f"{sc.OP_CHAT} {model}", kind=SpanKind.CLIENT) as span:
            span.set_attributes(
                {
                    sc.GEN_AI_OPERATION_NAME: sc.OP_CHAT,
                    sc.GEN_AI_PROVIDER_NAME: self.provider,
                    sc.GEN_AI_REQUEST_MODEL: model,
                    sc.GEN_AI_REQUEST_TEMPERATURE: temperature,
                    sc.GEN_AI_REQUEST_MAX_TOKENS: max_tokens,
                    sc.SERVER_ADDRESS: self._server_address,
                    sc.SERVER_PORT: self._server_port,
                    sc.SONDE_AGENT_STEP: step,
                }
            )
            if self.capture_content:
                span.set_attribute(sc.GEN_AI_INPUT_MESSAGES, json.dumps(to_semconv_messages(messages)))

            payload = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            body = self._post_with_retries(span, payload)

            choice = body["choices"][0]
            message = choice["message"]
            tool_calls = message.get("tool_calls") or []
            finish_reason = choice.get("finish_reason") or ""
            raw_usage = body.get("usage") or {}
            usage = Usage(
                input_tokens=int(raw_usage.get("prompt_tokens") or 0),
                output_tokens=int(raw_usage.get("completion_tokens") or 0),
                cache_read_input_tokens=int((raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
            )

            span.set_attributes(
                {
                    sc.GEN_AI_RESPONSE_ID: body.get("id", ""),
                    sc.GEN_AI_RESPONSE_MODEL: body.get("model", model),
                    sc.GEN_AI_RESPONSE_FINISH_REASONS: [finish_reason],
                    sc.GEN_AI_USAGE_INPUT_TOKENS: usage.input_tokens,
                    sc.GEN_AI_USAGE_OUTPUT_TOKENS: usage.output_tokens,
                    sc.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: usage.cache_read_input_tokens,
                }
            )
            if self.capture_content:
                output = to_semconv_messages([message])
                for item in output:
                    item["finish_reason"] = finish_reason
                span.set_attribute(sc.GEN_AI_OUTPUT_MESSAGES, json.dumps(output))

            history_message = {"role": "assistant", "content": message.get("content")}
            if tool_calls:
                history_message["tool_calls"] = tool_calls
            return ChatResult(message=history_message, finish_reason=finish_reason, usage=usage, tool_calls=tool_calls)

    def _post_with_retries(self, span: trace.Span, payload: dict) -> dict:
        retries = 0
        try:
            for attempt in range(self.max_retries + 1):
                try:
                    response = self._http.post("/chat/completions", json=payload)
                except httpx.TransportError as exc:
                    status, error_type, retry_after = None, type(exc).__name__, None
                else:
                    if response.status_code == 200:
                        return response.json()
                    status, error_type = response.status_code, str(response.status_code)
                    retry_after = response.headers.get("Retry-After")
                    if status not in RETRYABLE_STATUS:
                        break

                if attempt == self.max_retries:
                    break
                delay = self._backoff(attempt, retry_after)
                event = {"sonde.retry.attempt": attempt + 1, "sonde.retry.delay_s": round(delay, 3)}
                if status is not None:
                    event["http.response.status_code"] = status
                span.add_event(sc.EVENT_LLM_RETRY, event)
                retries += 1
                time.sleep(delay)
        finally:
            span.set_attribute(sc.SONDE_LLM_RETRIES, retries)

        span.set_attribute(sc.ERROR_TYPE, error_type)
        span.set_status(Status(StatusCode.ERROR, f"LLM request failed after {retries} retries: {error_type}"))
        raise LLMError(error_type, f"LLM request failed: {error_type}")

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return min(8.0, self.backoff_base_s * 2**attempt) * random.uniform(0.8, 1.2)
