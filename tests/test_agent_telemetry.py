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


def spans_named(spans, name):
    return [s for s in spans if s.name == name]


def test_sub_agent_runs_in_a_thread_inside_the_parent_trace(make_agent, span_exporter):
    result = make_agent("v1", delegation=True).run(
        "orders-api returns HTTP 500, investigate.", "conv-7", category="errors"
    )
    assert result.outcome == sc.OUTCOME_COMPLETED

    spans = span_exporter.get_finished_spans()
    (root,) = spans_named(spans, "invoke_agent sre-copilot")
    (delegation,) = spans_named(spans, "execute_tool ask_log_analyst")
    (sub,) = spans_named(spans, "invoke_agent log-analyst")

    assert sub.context.trace_id == root.context.trace_id
    assert sub.parent.span_id == delegation.context.span_id
    assert sub.attributes[sc.SONDE_AGENT_PARENT] == "sre-copilot"
    assert sub.attributes[sc.GEN_AI_CONVERSATION_ID] == "conv-7"
    assert sc.SONDE_AGENT_PARENT not in root.attributes

    sub_chats = [
        s for s in spans if s.parent and s.parent.span_id == sub.context.span_id and s.name.startswith("chat ")
    ]
    assert sub_chats and all(c.attributes[sc.GEN_AI_AGENT_NAME] == "log-analyst" for c in sub_chats)
    assert all(c.attributes[sc.GEN_AI_REQUEST_MODEL] == "sim-mini" for c in sub_chats)
    # the sub-agent only gets the tools it needs
    sub_tools = {
        s.attributes[sc.GEN_AI_TOOL_NAME]
        for s in spans
        if s.attributes.get(sc.GEN_AI_AGENT_NAME) == "log-analyst" and s.name.startswith("execute_tool")
    }
    assert sub_tools <= {"query_logs", "search_runbooks"}


def test_lost_context_detaches_the_sub_agent_but_keeps_the_conversation(make_agent, span_exporter):
    make_agent("v1", delegation=True, lost_context_rate=1).run(
        "orders-api returns HTTP 500, investigate.", "conv-8", category="errors"
    )
    spans = span_exporter.get_finished_spans()
    (root,) = spans_named(spans, "invoke_agent sre-copilot")
    (sub,) = spans_named(spans, "invoke_agent log-analyst")

    assert sub.parent is None  # a new, detached trace: what threads do without context propagation
    assert sub.context.trace_id != root.context.trace_id
    assert sub.attributes[sc.GEN_AI_CONVERSATION_ID] == "conv-8"  # what the data-quality check relies on
    assert sub.attributes[sc.SONDE_AGENT_PARENT] == "sre-copilot"


CONTENT_KEYS = {
    sc.GEN_AI_INPUT_MESSAGES,
    sc.GEN_AI_OUTPUT_MESSAGES,
    sc.GEN_AI_TOOL_CALL_ARGUMENTS,
    sc.GEN_AI_TOOL_CALL_RESULT,
}


def test_errors_capture_records_nothing_for_a_successful_run(make_agent, span_exporter):
    make_agent("v1", capture="errors").run("checkout-service p95 latency doubled.", "conv-9", category="latency")
    for span in span_exporter.get_finished_spans():
        assert not CONTENT_KEYS & set(span.attributes), span.name


def test_errors_capture_records_failures_only(make_agent, span_exporter, faults):
    faults.update({"loop_rate": 1, "hallucinated_tool_rate": 1})
    make_agent("v1", capture="errors").run("Something feels off in production.", "conv-10", category="vague")
    spans = span_exporter.get_finished_spans()
    (root,) = by_operation(spans, sc.OP_INVOKE_AGENT)
    transcript = json.loads(root.attributes[sc.GEN_AI_INPUT_MESSAGES])
    assert transcript[0]["role"] == "user" and len(transcript) > 8
    for chat in by_operation(spans, sc.OP_CHAT):
        assert not CONTENT_KEYS & set(chat.attributes)

    make_agent("v1", capture="errors").run(
        "Spike of 401 on auth-service, is it an attack?", "conv-11", category="security"
    )
    (blocked,) = spans_named(span_exporter.get_finished_spans(), "execute_tool block_ip_address")
    assert "203.0.113.42" in blocked.attributes[sc.GEN_AI_TOOL_CALL_ARGUMENTS]
    assert "tool_not_allowed" in blocked.attributes[sc.GEN_AI_TOOL_CALL_RESULT]


def test_capture_policy_from_environment(monkeypatch):
    from sonde_agent.capture import Capture, capture_from_env

    monkeypatch.delenv("SONDE_CAPTURE_CONTENT", raising=False)
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    assert capture_from_env() is Capture.OFF
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    assert capture_from_env() is Capture.ALL
    monkeypatch.setenv("SONDE_CAPTURE_CONTENT", "Errors")
    assert capture_from_env() is Capture.ERRORS


def test_completed_runs_sampled_for_evaluation_keep_question_and_answer_only(make_agent, span_exporter):
    task = "checkout-service p95 latency doubled, find out why."
    result = make_agent("v1", capture="errors", eval_sample_rate=1.0).run(task, "conv-20", category="latency")
    spans = span_exporter.get_finished_spans()
    (root,) = by_operation(spans, sc.OP_INVOKE_AGENT)
    assert root.attributes[sc.SONDE_EVAL_SAMPLED] is True
    question = json.loads(root.attributes[sc.GEN_AI_INPUT_MESSAGES])
    answer = json.loads(root.attributes[sc.GEN_AI_OUTPUT_MESSAGES])
    assert question == [{"role": "user", "parts": [{"type": "text", "content": task}]}]  # no system prompt
    assert answer[0]["parts"][0]["content"] == result.answer
    for span in spans:  # tool outputs and intermediate turns stay out
        if span is not root:
            assert not CONTENT_KEYS & set(span.attributes), span.name


def test_evaluation_sampling_respects_the_capture_policy_and_skips_failures(make_agent, span_exporter, faults):
    make_agent("v1", capture="off", eval_sample_rate=1.0).run("checkout-service p95 latency doubled.", "c1")
    make_agent("v1", capture="errors", eval_sample_rate=0.0).run("checkout-service p95 latency doubled.", "c2")
    faults.update({"loop_rate": 1})
    make_agent("v1", capture="errors", eval_sample_rate=1.0).run("Something feels off in production.", "c3")
    roots = by_operation(span_exporter.get_finished_spans(), sc.OP_INVOKE_AGENT)
    assert len(roots) == 3
    assert not any(r.attributes.get(sc.SONDE_EVAL_SAMPLED) for r in roots)


def test_sub_agent_runs_are_never_sampled_for_evaluation(make_agent, span_exporter):
    make_agent("v1", capture="all", delegation=True, eval_sample_rate=1.0).run(
        "orders-api returns HTTP 500, investigate.", "conv-21", category="errors"
    )
    roots = by_operation(span_exporter.get_finished_spans(), sc.OP_INVOKE_AGENT)
    sampled = [r.name for r in roots if r.attributes.get(sc.SONDE_EVAL_SAMPLED)]
    assert len(roots) == 2 and sampled == ["invoke_agent sre-copilot"]
