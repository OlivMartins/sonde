"""The agent must emit a span tree that follows the OTel GenAI conventions."""

from __future__ import annotations

import json

from opentelemetry.trace import SpanKind, StatusCode

from sonde_agent import semconv as sc

RUN_KEYS = (sc.GEN_AI_AGENT_NAME, sc.GEN_AI_AGENT_VERSION, sc.GEN_AI_CONVERSATION_ID, sc.SONDE_TASK_CATEGORY)


def by_operation(spans, operation):
    return [s for s in spans if s.attributes.get(sc.GEN_AI_OPERATION_NAME) == operation]


def test_completed_run_produces_semconv_span_tree(make_agent, span_exporter):
    agent = make_agent("v1")
    result = agent.run("checkout-service p95 latency doubled, find out why.", "conv-1", category="latency")

    assert result.outcome == sc.OUTCOME_COMPLETED
    assert result.steps == 4  # three tool rounds, then the answer
    assert result.feedback == 1

    spans = span_exporter.get_finished_spans()
    (root,) = by_operation(spans, sc.OP_INVOKE_AGENT)
    chats = sorted(by_operation(spans, sc.OP_CHAT), key=lambda s: s.attributes[sc.SONDE_AGENT_STEP])
    tools = by_operation(spans, sc.OP_EXECUTE_TOOL)
    http = [s for s in spans if s.name == "POST"]

    assert root.name == "invoke_agent sre-copilot"
    assert root.kind == SpanKind.INTERNAL
    assert root.attributes[sc.GEN_AI_AGENT_ID] == "sre-copilot@v1"
    assert root.attributes[sc.SONDE_AGENT_OUTCOME] == "completed"
    assert root.status.status_code != StatusCode.ERROR

    assert [c.attributes[sc.SONDE_AGENT_STEP] for c in chats] == [1, 2, 3, 4]
    for chat in chats:
        assert chat.name == "chat sim-large"
        assert chat.kind == SpanKind.CLIENT
        assert chat.parent.span_id == root.context.span_id
        assert chat.attributes[sc.GEN_AI_PROVIDER_NAME] == "fake-llm"
        assert chat.attributes[sc.GEN_AI_REQUEST_MODEL] == "sim-large"
        assert chat.attributes[sc.GEN_AI_RESPONSE_MODEL] == "sim-large-2026-06-01"
        assert chat.attributes[sc.GEN_AI_USAGE_INPUT_TOKENS] > 0
        assert chat.attributes[sc.GEN_AI_USAGE_OUTPUT_TOKENS] > 0
        assert chat.attributes[sc.SERVER_ADDRESS] == "127.0.0.1"
    assert chats[0].attributes[sc.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS] == 0
    assert all(c.attributes[sc.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS] > 0 for c in chats[1:])
    assert chats[-1].attributes[sc.GEN_AI_RESPONSE_FINISH_REASONS] == ("stop",)

    assert [t.name for t in tools] == [
        "execute_tool get_service_metrics",
        "execute_tool query_logs",
        "execute_tool search_runbooks",
    ]
    assert all(t.parent.span_id == root.context.span_id for t in tools)

    chat_ids = {c.context.span_id for c in chats}
    assert len(http) == 4 and all(h.parent.span_id in chat_ids for h in http)

    # Run-level attributes are stamped on every span, HTTP spans included.
    assert len({s.context.trace_id for s in spans}) == 1
    for span in spans:
        assert span.attributes[sc.GEN_AI_CONVERSATION_ID] == "conv-1"
        assert all(key in span.attributes for key in RUN_KEYS), span.name

    # The agent span carries the run totals.
    for key in (sc.GEN_AI_USAGE_INPUT_TOKENS, sc.GEN_AI_USAGE_OUTPUT_TOKENS, sc.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS):
        assert root.attributes[key] == sum(c.attributes[key] for c in chats)


def test_hallucinated_tool_is_blocked_and_traced(make_agent, span_exporter, faults):
    faults.update({"hallucinated_tool_rate": 1})
    result = make_agent("v1").run("Spike of 401 on auth-service, is it an attack?", "conv-2", category="security")

    assert result.outcome == sc.OUTCOME_COMPLETED
    assert result.blocked_tools == ["block_ip_address"]
    (blocked,) = [s for s in span_exporter.get_finished_spans() if s.name == "execute_tool block_ip_address"]
    assert blocked.attributes[sc.SONDE_TOOL_BLOCKED] is True
    assert blocked.attributes[sc.ERROR_TYPE] == "tool_not_allowed"
    assert blocked.status.status_code == StatusCode.ERROR


def test_rate_limits_are_retried_then_reported(make_agent, span_exporter, faults):
    faults.update({"rate_limit_rate": 1})
    result = make_agent("v2").run("orders-api returns HTTP 500, investigate.", "conv-3", category="errors")

    assert result.outcome == sc.OUTCOME_LLM_ERROR
    assert result.error_type == "429"
    spans = span_exporter.get_finished_spans()
    (chat,) = by_operation(spans, sc.OP_CHAT)
    assert chat.attributes[sc.SONDE_LLM_RETRIES] == 3
    assert [e.name for e in chat.events].count(sc.EVENT_LLM_RETRY) == 3
    assert chat.attributes[sc.ERROR_TYPE] == "429"
    assert chat.status.status_code == StatusCode.ERROR
    assert len([s for s in spans if s.name == "POST" and s.parent.span_id == chat.context.span_id]) == 4

    (root,) = by_operation(spans, sc.OP_INVOKE_AGENT)
    assert root.attributes[sc.ERROR_TYPE] == "429"
    assert root.status.status_code == StatusCode.ERROR


def test_vague_task_loops_until_max_steps(make_agent, faults):
    faults.update({"loop_rate": 1})
    result = make_agent("v1").run("Something feels off in production.", "conv-4", category="vague")

    assert result.outcome == sc.OUTCOME_MAX_STEPS
    assert result.steps == 8
    assert result.tool_calls == 8


def test_content_capture_is_opt_in(make_agent, span_exporter):
    make_agent("v1", capture=False).run("checkout-service p95 latency doubled.", "conv-5", category="latency")

    content_keys = {
        sc.GEN_AI_INPUT_MESSAGES,
        sc.GEN_AI_OUTPUT_MESSAGES,
        sc.GEN_AI_TOOL_CALL_ARGUMENTS,
        sc.GEN_AI_TOOL_CALL_RESULT,
    }
    for span in span_exporter.get_finished_spans():
        assert not content_keys & set(span.attributes), span.name


def test_captured_messages_follow_semconv_format(make_agent, span_exporter):
    make_agent("v1").run("checkout-service p95 latency doubled.", "conv-6", category="latency")

    chats = sorted(
        by_operation(span_exporter.get_finished_spans(), sc.OP_CHAT), key=lambda s: s.attributes[sc.SONDE_AGENT_STEP]
    )
    last_input = json.loads(chats[-1].attributes[sc.GEN_AI_INPUT_MESSAGES])
    assert [m["role"] for m in last_input][:2] == ["user", "assistant"]  # system prompt is not duplicated
    part_types = {p["type"] for m in last_input for p in m["parts"]}
    assert part_types == {"text", "tool_call", "tool_call_response"}

    output = json.loads(chats[-1].attributes[sc.GEN_AI_OUTPUT_MESSAGES])
    assert output[0]["role"] == "assistant"
    assert output[0]["finish_reason"] == "stop"
    assert output[0]["parts"][0]["type"] == "text"
