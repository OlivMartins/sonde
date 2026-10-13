"""Simulated SRE tools and a policy-enforcing tool registry.

Tool results deliberately contain e-mail addresses, API keys and bearer
tokens, as real logs do. The OpenTelemetry Collector masks them before they
reach ClickHouse (see otel-collector/config.yaml).
"""

from __future__ import annotations

import json
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import semconv as sc

tracer = trace.get_tracer("sonde_agent.tools")


class ToolTimeoutError(Exception):
    pass


class BackendUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., dict]
    side_effect: bool = False

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


RUNBOOKS = [
    (
        "RB-102",
        "High latency after deploy",
        "Compare p95 before/after deploy, check slow queries, roll back if needed.",
    ),
    ("RB-118", "Database connection saturation", "Check pool usage, long transactions and replica lag."),
    ("RB-204", "5xx from upstream exhaustion", "Inspect connection pools, add circuit breakers, scale upstream."),
    ("RB-230", "Error budget burn", "Freeze deploys, page the owning team, open an incident."),
    ("RB-311", "Kafka consumer lag", "Scale consumers up to partition count, check rebalance storms."),
    ("RB-340", "Disk pressure on storage nodes", "Check TTL jobs, merges backlog and large parts."),
    ("RB-512", "Credential stuffing", "Rate limit by IP and account, enforce MFA, involve security on-call."),
    ("RB-530", "Leaked API key", "Revoke the key, rotate secrets, search audit logs for usage."),
]

USERS = ["jane.doe@example.com", "j.martin@example.org", "ops-bot@example.net", "a.nguyen@example.com"]


def _sleep_ms(rng: random.Random, low: int, high: int) -> None:
    time.sleep(rng.uniform(low, high) / 1000)


def build_tools(rng: random.Random) -> list[Tool]:
    def get_service_metrics(service: str, window_minutes: int = 30, **_: object) -> dict:
        _sleep_ms(rng, 40, 250)
        if rng.random() < 0.02:
            raise BackendUnavailableError("metrics backend returned 503")
        p50 = rng.uniform(20, 120)
        return {
            "service": service,
            "window_minutes": window_minutes,
            "p50_ms": round(p50, 1),
            "p95_ms": round(p50 * rng.uniform(2.5, 9), 1),
            "error_rate": round(rng.uniform(0, 0.06), 4),
            "rps": round(rng.uniform(50, 1500), 1),
        }

    def query_logs(service: str, level: str = "error", limit: int = 20, **_: object) -> dict:
        _sleep_ms(rng, 150, 900)
        if rng.random() < 0.04:
            raise ToolTimeoutError("log query exceeded the 2s budget")
        sample = [
            f"2026-10-13T20:41:07Z {level.upper()} {service} payment declined for user={rng.choice(USERS)}",
            f"2026-10-13T20:41:09Z {level.upper()} {service} upstream call failed: connection pool exhausted",
        ]
        if rng.random() < 0.3:
            sample.append(f"2026-10-13T20:41:12Z WARN {service} retrying with api_key=demo_{uuid.uuid4().hex[:16]}")
        if rng.random() < 0.2:
            sample.append(
                f"2026-10-13T20:41:15Z DEBUG {service} outbound headers Authorization: Bearer demo{uuid.uuid4().hex}"
            )
        return {"service": service, "level": level, "matches": rng.randint(3, 4000), "sample": sample[:limit]}

    def search_runbooks(query: str, **_: object) -> dict:
        _sleep_ms(rng, 60, 300)
        picks = rng.sample(RUNBOOKS, 3)
        return {"query": query, "results": [{"id": i, "title": t, "summary": s} for i, t, s in picks]}

    def create_incident_ticket(title: str, severity: str = "SEV3", **_: object) -> dict:
        _sleep_ms(rng, 100, 400)
        if rng.random() < 0.01:
            raise BackendUnavailableError("ticketing API timeout")
        return {"ticket_id": f"INC-{rng.randint(1000, 9999)}", "title": title, "severity": severity, "status": "open"}

    string = {"type": "string"}
    return [
        Tool(
            "get_service_metrics",
            "Return latency, error rate and throughput for a service over a time window.",
            {
                "type": "object",
                "properties": {"service": string, "window_minutes": {"type": "integer"}},
                "required": ["service"],
            },
            get_service_metrics,
        ),
        Tool(
            "query_logs",
            "Search recent logs of a service by level.",
            {
                "type": "object",
                "properties": {"service": string, "level": string, "limit": {"type": "integer"}},
                "required": ["service"],
            },
            query_logs,
        ),
        Tool(
            "search_runbooks",
            "Full-text search in the SRE runbooks.",
            {"type": "object", "properties": {"query": string}, "required": ["query"]},
            search_runbooks,
        ),
        Tool(
            "create_incident_ticket",
            "Open an incident ticket. Has side effects.",
            {
                "type": "object",
                "properties": {"title": string, "severity": {"type": "string", "enum": ["SEV1", "SEV2", "SEV3"]}},
                "required": ["title"],
            },
            create_incident_ticket,
            side_effect=True,
        ),
    ]


class ToolRegistry:
    """Executes tool calls and enforces an allow-list.

    A model can request a tool it was never given (hallucination or prompt
    injection). Those calls are refused, traced with error.type
    `tool_not_allowed` and returned to the model as an error.
    """

    def __init__(self, tools: list[Tool], capture_content: bool = False):
        self._tools = {tool.name: tool for tool in tools}
        self.capture_content = capture_content

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, call: dict) -> str:
        function = call.get("function", {})
        name = function.get("name", "")
        raw_arguments = function.get("arguments") or "{}"
        with tracer.start_as_current_span(f"{sc.OP_EXECUTE_TOOL} {name}", kind=SpanKind.INTERNAL) as span:
            span.set_attributes(
                {
                    sc.GEN_AI_OPERATION_NAME: sc.OP_EXECUTE_TOOL,
                    sc.GEN_AI_TOOL_NAME: name,
                    sc.GEN_AI_TOOL_CALL_ID: call.get("id", ""),
                    sc.GEN_AI_TOOL_TYPE: "function",
                    sc.SONDE_TOOL_BLOCKED: False,
                }
            )
            if self.capture_content:
                span.set_attribute(sc.GEN_AI_TOOL_CALL_ARGUMENTS, raw_arguments)

            tool = self._tools.get(name)
            if tool is None:
                span.set_attribute(sc.SONDE_TOOL_BLOCKED, True)
                return self._fail(span, "tool_not_allowed", f"tool '{name}' is not allowed by policy")
            span.set_attribute(sc.GEN_AI_TOOL_DESCRIPTION, tool.description)

            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise TypeError("arguments must be a JSON object")
            except (json.JSONDecodeError, TypeError) as exc:
                return self._fail(span, "invalid_arguments", str(exc))

            try:
                result = json.dumps(tool.handler(**arguments))
            except Exception as exc:  # noqa: BLE001 - tool failures are reported to the model
                span.record_exception(exc)
                return self._fail(span, type(exc).__name__, str(exc))

            if self.capture_content:
                span.set_attribute(sc.GEN_AI_TOOL_CALL_RESULT, result)
            return result

    def _fail(self, span: trace.Span, error_type: str, message: str) -> str:
        span.set_attribute(sc.ERROR_TYPE, error_type)
        span.set_status(Status(StatusCode.ERROR, message))
        result = json.dumps({"error": error_type, "message": message})
        if self.capture_content:
            span.set_attribute(sc.GEN_AI_TOOL_CALL_RESULT, result)
        return result
