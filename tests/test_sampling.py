"""Tail sampling thins the raw spans, never the numbers.

The replay is written as the sampling profile (docker-compose.sampling.yml)
would write it: every span to otel_spans_stream, and only the traces the
gateway keeps to otel_traces.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.trace import StatusCode

from clickhouse_support import ROOT, Redaction, expand_grafana_macros, kept_by_tail_sampling, new_session, query, store
from sonde_agent import semconv as sc
from sonde_agent.evaluator import candidates_sql

pytest.importorskip("chdb")

DASHBOARD = ROOT / "grafana" / "dashboards" / "sonde-agents.json"


@pytest.fixture(scope="module")
def sampled(replayed_spans, tmp_path_factory):
    kept = kept_by_tail_sampling(replayed_spans)
    session = new_session(tmp_path_factory.mktemp("chdb-sampled"))
    store(session, replayed_spans, Redaction(), raw_trace_ids=kept)
    yield session, kept
    session.close()


def operation(spans, name):
    return [s for s in spans if s.attributes.get(sc.GEN_AI_OPERATION_NAME) == name]


def test_raw_spans_are_sampled_but_the_numbers_stay_exact(sampled, replayed_spans):
    ch, kept = sampled
    traces = {s.context.trace_id for s in replayed_spans}
    raw = query(ch, "SELECT count() AS n FROM otel.otel_traces")[0]["n"]
    assert 0 < len(kept) < len(traces)
    assert raw == sum(1 for s in replayed_spans if s.context.trace_id in kept) < len(replayed_spans)

    chats = operation(replayed_spans, sc.OP_CHAT)
    truth = {
        "SELECT count() AS v FROM otel.genai_agent_runs": len(operation(replayed_spans, sc.OP_INVOKE_AGENT)),
        "SELECT count() AS v FROM otel.genai_llm_calls": len(chats),
        "SELECT count() AS v FROM otel.genai_tool_calls": len(operation(replayed_spans, sc.OP_EXECUTE_TOOL)),
        "SELECT sum(InputTokens) AS v FROM otel.genai_llm_calls": sum(
            s.attributes.get(sc.GEN_AI_USAGE_INPUT_TOKENS, 0) for s in chats
        ),
        "SELECT sum(Calls) AS v FROM otel.genai_usage_1m": len(chats),
        "SELECT sum(Spans) AS v FROM otel.span_quality_1m": len(replayed_spans),
    }
    for sql, expected in truth.items():
        assert int(query(ch, sql)[0]["v"]) == expected, sql
    priced = query(ch, "SELECT sum(CostUsd) AS c FROM otel.genai_llm_calls_priced")[0]["c"]
    assert priced > 0 and query(ch, "SELECT sum(CostUsd) AS c FROM otel.genai_cost_1m")[0]["c"] == pytest.approx(priced)


def test_every_trace_worth_investigating_is_kept(sampled, replayed_spans):
    ch, kept = sampled
    must_keep = {
        s.context.trace_id
        for s in replayed_spans
        if s.status.status_code == StatusCode.ERROR
        or s.attributes.get(sc.SONDE_TOOL_BLOCKED)
        or s.attributes.get(sc.SONDE_EVAL_SAMPLED)
    }
    assert must_keep and must_keep <= kept
    failed = query(
        ch,
        """
        SELECT count() AS n FROM otel.genai_agent_runs
        WHERE Outcome != 'completed' AND TraceId NOT IN (SELECT TraceId FROM otel.otel_traces_trace_id_ts)""",
    )
    assert failed[0]["n"] == 0
    # the evaluator reads the raw table: it still finds every run it has to judge
    candidates = query(ch, candidates_sql(1000))
    assert len(candidates) == len([s for s in replayed_spans if s.attributes.get(sc.SONDE_EVAL_SAMPLED)])


def test_investigation_panels_point_at_kept_traces(sampled):
    ch, kept = sampled
    panels = {p["title"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    now = datetime.now(UTC)

    def run(title):
        sql = panels[title]["targets"][0]["rawSql"]
        return query(ch, expand_grafana_macros(sql, now - timedelta(hours=1), now + timedelta(hours=1)))

    expensive = run("Exécutions les plus chères (cliquez un identifiant de trace)")
    assert expensive and {r["trace_kept"] for r in expensive} <= {"oui", "non"}
    for r in expensive:
        assert (r["trace_kept"] == "oui") == (int(r["trace_id"], 16) in kept)
    trace = run("Trace (dernière exécution gardée, ou celle que vous avez cliquée)")
    assert trace and int(trace[0]["traceID"], 16) in kept
