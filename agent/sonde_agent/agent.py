"""A small tool-calling agent loop, traced with OpenTelemetry GenAI conventions.

Span tree of one run:

    invoke_agent sre-copilot            (INTERNAL)
    ├── chat sim-large                  (CLIENT)  step 1
    │   └── POST                        (CLIENT)  httpx auto-instrumentation, one per attempt
    ├── execute_tool get_service_metrics (INTERNAL)
    ├── chat sim-large                  (CLIENT)  step 2
    └── ...
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import semconv as sc
from .capture import Capture
from .llm import LLMClient, LLMError, Usage, to_semconv_messages
from .telemetry import run_attributes
from .tools import ToolRegistry

tracer = trace.get_tracer("sonde_agent.agent")

SYSTEM_PROMPT_V1 = """You are an SRE copilot for an e-commerce platform.
Investigate production issues step by step using the tools you are given.
Always gather evidence (metrics, logs) before concluding, cite the runbook you
relied on, and only open an incident ticket when user impact is confirmed.
Never take destructive or blocking actions yourself: recommend them.

Platform overview:
- checkout-service (Go) calls payments-api, orders-api and inventory-db.
- payments-api (Java) depends on an external PSP and auth-service.
- orders-api (Python) writes to PostgreSQL and publishes to Kafka topic orders.
- search-api (Rust) reads from ClickHouse.
- Logs are shipped by Vector to Kafka then to ClickHouse; traces go through the
  OpenTelemetry Collector.

Answer format: root cause, evidence, recommended action, runbook id.
""" + "\n".join(
    f"Runbook note {i}: keep the investigation scoped, prefer reversible actions, "
    "and record every hypothesis you discard."
    for i in range(1, 25)
)

# v2 is the "prompt diet" variant: same instructions, no padding.
SYSTEM_PROMPT_V2 = SYSTEM_PROMPT_V1.split("Platform overview:")[0]


@dataclass(frozen=True)
class AgentConfig:
    name: str = "sre-copilot"
    version: str = "v1"
    model: str = "sim-large"
    system_prompt: str = SYSTEM_PROMPT_V1
    max_steps: int = 8
    temperature: float = 0.2
    max_tokens: int = 1024
    description: str = "Investigates production incidents with metrics, logs and runbooks."


@dataclass
class RunResult:
    outcome: str
    steps: int
    tool_calls: int
    usage: Usage
    answer: str
    trace_id: str
    error_type: str | None = None
    feedback: int | None = None
    blocked_tools: list[str] = field(default_factory=list)


FeedbackFn = Callable[[str], int | None]


class Agent:
    def __init__(self, config: AgentConfig, llm: LLMClient, tools: ToolRegistry, feedback_fn: FeedbackFn | None = None):
        self.config = config
        self.llm = llm
        self.tools = tools
        self.feedback_fn = feedback_fn

    def run(
        self, task: str, conversation_id: str, category: str = "unknown", parent_agent: str | None = None
    ) -> RunResult:
        """Run the agent loop. `parent_agent` is set when this agent runs as a sub-agent."""
        cfg = self.config
        span_attributes = {
            sc.GEN_AI_OPERATION_NAME: sc.OP_INVOKE_AGENT,
            sc.GEN_AI_AGENT_ID: f"{cfg.name}@{cfg.version}",
            sc.GEN_AI_AGENT_DESCRIPTION: cfg.description,
            sc.GEN_AI_PROVIDER_NAME: self.llm.provider,
            sc.GEN_AI_REQUEST_MODEL: cfg.model,
        }
        if parent_agent:
            span_attributes[sc.SONDE_AGENT_PARENT] = parent_agent
        attributes = {
            sc.GEN_AI_AGENT_NAME: cfg.name,
            sc.GEN_AI_AGENT_VERSION: cfg.version,
            sc.GEN_AI_CONVERSATION_ID: conversation_id,
            sc.SONDE_TASK_CATEGORY: category,
        }
        with (
            run_attributes(attributes),
            tracer.start_as_current_span(
                f"{sc.OP_INVOKE_AGENT} {cfg.name}",
                kind=SpanKind.INTERNAL,
                attributes=span_attributes,
            ) as span,
        ):
            messages: list[dict] = [
                {"role": "system", "content": cfg.system_prompt},
                {"role": "user", "content": task},
            ]
            result = self._loop(messages)
            result.trace_id = format(span.get_span_context().trace_id, "032x")

            span.set_attributes(
                {
                    sc.SONDE_AGENT_OUTCOME: result.outcome,
                    sc.SONDE_AGENT_STEPS: result.steps,
                    sc.SONDE_AGENT_TOOL_CALLS: result.tool_calls,
                    sc.GEN_AI_USAGE_INPUT_TOKENS: result.usage.input_tokens,
                    sc.GEN_AI_USAGE_OUTPUT_TOKENS: result.usage.output_tokens,
                    sc.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: result.usage.cache_read_input_tokens,
                }
            )
            if result.outcome != sc.OUTCOME_COMPLETED:
                result.error_type = result.error_type or result.outcome
                span.set_attribute(sc.ERROR_TYPE, result.error_type)
                span.set_status(Status(StatusCode.ERROR, result.outcome))
                if self.llm.capture is Capture.ERRORS:
                    # The whole conversation, recorded once, only for runs that went wrong.
                    span.set_attribute(sc.GEN_AI_INPUT_MESSAGES, json.dumps(to_semconv_messages(messages)))

            if self.feedback_fn is not None:
                result.feedback = self.feedback_fn(result.outcome)
                if result.feedback is not None:
                    span.set_attribute(sc.SONDE_FEEDBACK_SCORE, result.feedback)
            return result

    def _loop(self, messages: list[dict]) -> RunResult:
        cfg = self.config
        result = RunResult(outcome=sc.OUTCOME_MAX_STEPS, steps=0, tool_calls=0, usage=Usage(), answer="", trace_id="")

        for step in range(1, cfg.max_steps + 1):
            result.steps = step
            try:
                reply = self.llm.chat(
                    model=cfg.model,
                    messages=messages,
                    tools=self.tools.schemas(),
                    temperature=cfg.temperature,
                    max_tokens=cfg.max_tokens,
                    step=step,
                )
            except LLMError as exc:
                result.outcome = sc.OUTCOME_LLM_ERROR
                result.error_type = exc.error_type
                return result

            result.usage += reply.usage
            messages.append(reply.message)

            if not reply.tool_calls:
                result.outcome = sc.OUTCOME_COMPLETED
                result.answer = reply.message.get("content") or ""
                return result

            for call in reply.tool_calls:
                output = self.tools.execute(call)
                result.tool_calls += 1
                if '"tool_not_allowed"' in output:
                    result.blocked_tools.append(call.get("function", {}).get("name", ""))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})

        return result
