"""End-to-end check of the ClickHouse layer, without a server.

Agent runs are replayed into chDB (embedded ClickHouse) exactly as the
collector would write them: same columns, same attribute encoding, same
redaction. Then the materialized views, the cost views and every Grafana
panel query are executed.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from clickhouse_support import (
    ROOT,
    Redaction,
    expand_grafana_macros,
    insert_rows,
    log_to_row,
    new_session,
    query,
    store,
)
from sonde_agent import semconv as sc
from sonde_agent.evaluator import Evaluator, candidates_sql
from sonde_agent.llm import LLMClient

pytest.importorskip("chdb")

DASHBOARD = ROOT / "grafana" / "dashboards" / "sonde-agents.json"
ALERTS = ROOT / "grafana" / "provisioning" / "alerting" / "sonde-alerts.yaml"
from conftest import REPLAY_RUNS as RUNS  # noqa: E402


@pytest.fixture(scope="module")
def spans(replayed_spans):
    """What reaches ClickHouse: two LLM-call spans are dropped on purpose, as an
    exporter losing data would, so their HTTP child spans become orphans."""
    chats_with_children = {s.parent.span_id for s in replayed_spans if s.name == "POST" and s.parent}
    dropped = {
        s.context.span_id
        for s in replayed_spans
        if s.attributes.get(sc.GEN_AI_OPERATION_NAME) == sc.OP_CHAT and s.context.span_id in chats_with_children
    }
    dropped = set(sorted(dropped)[:2])
    return [s for s in replayed_spans if s.context.span_id not in dropped]


@pytest.fixture(scope="module")
def world(spans, tmp_path_factory, span_exporter, log_exporter, logger_provider, fake_llm_url):
    """ClickHouse after the agents ran, then after the evaluator judged the sampled runs."""
    import fake_llm

    session = new_session(tmp_path_factory.mktemp("chdb"))
    redaction = Redaction()
    store(session, spans, redaction)

    original = fake_llm.Faults.snapshot()
    fake_llm.Faults.update({"latency_factor": 0, "rate_limit_rate": 0, "error_rate": 0})
    span_exporter.clear()
    log_exporter.clear()
    llm = LLMClient(fake_llm_url, provider="fake-llm", backoff_base_s=0)
    evaluator = Evaluator(llm, "sim-judge", lambda sql: query(session, sql), logger_provider)
    emitted = evaluator.run_until_idle()
    llm.close()
    fake_llm.Faults.update(original)

    judge_spans = list(span_exporter.get_finished_spans())
    logs = list(log_exporter.get_finished_logs())
    span_exporter.clear()
    store(session, judge_spans, redaction)
    insert_rows(session, "otel.otel_logs", [log_to_row(record, redaction) for record in logs])
    yield SimpleNamespace(
        session=session, spans=spans + judge_spans, judge_spans=judge_spans, logs=logs, emitted=emitted
    )
    session.close()


@pytest.fixture(scope="module")
def ch(world):
    return world.session


@pytest.fixture(scope="module")
def stored(world):
    """Every span that reached ClickHouse: agents and evaluator."""
    return world.spans


def count_operation(spans, operation: str) -> int:
    return sum(1 for s in spans if s.attributes.get(sc.GEN_AI_OPERATION_NAME) == operation)


def test_materialized_views_type_every_genai_span(ch, stored):
    spans = stored
    assert query(ch, "SELECT count() AS n FROM otel.otel_traces")[0]["n"] == len(spans)
    invoke_spans = count_operation(spans, sc.OP_INVOKE_AGENT)
    assert query(ch, "SELECT count() AS n FROM otel.genai_agent_runs")[0]["n"] == invoke_spans
    assert query(ch, "SELECT count() AS n FROM otel.genai_agent_runs WHERE ParentAgent = ''")[0]["n"] == RUNS
    sub_runs = query(ch, "SELECT DISTINCT AgentName, ParentAgent FROM otel.genai_agent_runs WHERE ParentAgent != ''")
    assert sub_runs == [{"AgentName": "log-analyst", "ParentAgent": "sre-copilot"}]
    assert query(ch, "SELECT count() AS n FROM otel.genai_llm_calls")[0]["n"] == count_operation(spans, sc.OP_CHAT)
    assert query(ch, "SELECT count() AS n FROM otel.genai_tool_calls")[0]["n"] == count_operation(
        spans, sc.OP_EXECUTE_TOOL
    )

    runs = query(ch, "SELECT * FROM otel.genai_agent_runs")
    assert {r["AgentVersion"] for r in runs} == {"v1", "v2"}
    assert {r["Outcome"] for r in runs} <= {"completed", "max_steps_exceeded", "llm_error"}
    assert {r["AgentName"] for r in runs} == {"sre-copilot", "log-analyst"}
    assert all(r["Steps"] >= 1 for r in runs)
    assert sum(r["InputTokens"] for r in runs) > 0

    calls = query(ch, "SELECT FinishReason, Provider, ResponseModel, Step FROM otel.genai_llm_calls")
    assert {c["FinishReason"] for c in calls} <= {"stop", "tool_calls", ""}
    assert "tool_calls" in {c["FinishReason"] for c in calls}
    assert all(c["Provider"] == "fake-llm" and c["Step"] >= 1 for c in calls)


def test_rollup_matches_detail_tables(ch):
    rollup = query(
        ch,
        """
        SELECT sum(Calls) AS calls, sum(InputTokens) AS input, sum(OutputTokens) AS output,
               quantilesMerge(0.5, 0.95, 0.99)(LatencyMs) AS q
        FROM otel.genai_usage_1m""",
    )[0]
    detail = query(
        ch, "SELECT count() AS calls, sum(InputTokens) AS input, sum(OutputTokens) AS output FROM otel.genai_llm_calls"
    )[0]
    assert int(rollup["calls"]) == int(detail["calls"])
    assert int(rollup["input"]) == int(detail["input"])
    assert int(rollup["output"]) == int(detail["output"])
    assert len(rollup["q"]) == 3


def test_cost_is_priced_with_asof_join_and_consistent(ch):
    priced = query(ch, "SELECT sum(CostUsd) AS cost, min(Priced) AS all_priced FROM otel.genai_llm_calls_priced")[0]
    per_minute = query(ch, "SELECT sum(CostUsd) AS cost FROM otel.genai_cost_1m")[0]
    assert priced["all_priced"] == 1 or priced["all_priced"] is True
    assert priced["cost"] > 0
    assert per_minute["cost"] == pytest.approx(priced["cost"], rel=1e-9)

    # A price change only applies from its effective date: history is unchanged.
    before = per_minute["cost"]
    ch.query("INSERT INTO otel.genai_model_pricing VALUES ('sim-large', now() + INTERVAL 1 DAY, 100, 100, 100)")
    assert query(ch, "SELECT sum(CostUsd) AS cost FROM otel.genai_cost_1m")[0]["cost"] == pytest.approx(
        before, rel=1e-9
    )


def test_secrets_are_masked_but_json_stays_valid(ch):
    rows = query(
        ch,
        """
        SELECT SpanAttributes['gen_ai.input.messages'] AS input, SpanAttributes['gen_ai.tool.call.result'] AS result
        FROM otel.otel_traces
        WHERE SpanAttributes['gen_ai.operation.name'] IN ('chat', 'execute_tool')""",
    )
    blobs = [v for r in rows for v in (r["input"], r["result"]) if v]
    assert blobs
    joined = "\n".join(blobs)
    assert "@example." not in joined and "api_key=demo_" not in joined and "Bearer demo" not in joined
    assert "****" in joined
    for blob in blobs:
        decoded = json.loads(blob)  # masking must not break the JSON payload
        for message in decoded if isinstance(decoded, list) else []:
            for part in message["parts"]:
                if part["type"] == "tool_call_response" and part["response"].startswith("{"):
                    json.loads(part["response"])

    masked = query(
        ch, "SELECT sum(toUInt64OrZero(SpanAttributes['redaction.masked.count'])) AS n FROM otel.otel_traces"
    )
    assert int(masked[0]["n"]) > 0


def test_evaluations_land_in_clickhouse_attached_to_their_runs(world, ch):
    sampled = [s for s in world.spans if s.attributes.get(sc.SONDE_EVAL_SAMPLED)]
    assert sampled and world.emitted == len(sampled) == len(world.logs)
    rows = query(
        ch,
        """
        SELECT r.AgentName AS agent, r.ParentAgent AS parent, r.Outcome AS outcome, e.JudgeModel AS judge
        FROM otel.genai_evaluations AS e
        INNER JOIN otel.genai_agent_runs AS r ON e.TraceId = r.TraceId AND e.SpanId = r.SpanId""",
    )
    assert len(rows) == len(sampled)
    assert {(r["agent"], r["parent"], r["outcome"], r["judge"]) for r in rows} == {
        ("sre-copilot", "", "completed", "sim-judge")
    }
    # nothing left to judge on the next poll
    assert query(ch, candidates_sql(1000)) == []
    # the judge has a cost, tracked like any agent's
    judge = query(ch, "SELECT sum(CostUsd) AS cost FROM otel.genai_cost_1m WHERE AgentName = 'sonde-evaluator'")[0]
    assert judge["cost"] > 0


def test_judge_scores_follow_answer_quality(ch):
    rows = query(
        ch,
        """
        SELECT t.SpanAttributes['gen_ai.output.messages'] AS answer, s.Score AS score, s.Label AS label
        FROM otel.genai_run_scores AS s
        INNER JOIN otel.otel_traces AS t ON t.TraceId = s.TraceId
        WHERE t.SpanAttributes['sonde.eval.sampled'] = 'true'""",
    )
    shallow = [r["score"] for r in rows if "seems related to recent changes" in r["answer"]]
    with_runbook = [r["score"] for r in rows if "RB-" in r["answer"]]
    assert shallow and with_runbook
    assert max(shallow) < 2.5 < 3.8 <= min(with_runbook)
    assert {r["label"] for r in rows} == {"pass", "fail"}


def test_cost_per_good_answer_is_never_below_cost_per_success(ch):
    (panel,) = [p for p in json.loads(DASHBOARD.read_text())["panels"] if p["title"].startswith("Test A/B")]
    now = datetime.now(UTC)
    sql = expand_grafana_macros(panel["targets"][0]["rawSql"], now - timedelta(hours=1), now + timedelta(hours=1))
    rows = query(ch, sql)
    assert [r["version"] for r in rows] == ["v1", "v2"]
    for r in rows:
        assert r["judged"] > 0 and 1 <= r["judge_score"] <= 5
        if r["usd_per_good_answer"] is not None:
            assert r["usd_per_good_answer"] >= r["usd_per_success"]


def dashboard_queries():
    dashboard = json.loads(DASHBOARD.read_text())
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            yield pytest.param(panel, target, id=panel["title"])


@pytest.mark.parametrize(("panel", "target"), list(dashboard_queries()))
def test_every_dashboard_panel_query_runs(ch, panel, target):
    now = datetime.now(UTC)
    sql = expand_grafana_macros(target["rawSql"], now - timedelta(hours=1), now + timedelta(hours=1))
    assert "$__" not in sql and "${" not in sql
    rows = query(ch, sql)
    assert rows, f"panel '{panel['title']}' returned no rows"

    if panel["type"] == "traces":
        expected = {
            "traceID",
            "spanID",
            "parentSpanID",
            "serviceName",
            "operationName",
            "startTime",
            "duration",
            "tags",
            "serviceTags",
            "statusCode",
        }
        assert expected <= set(rows[0])
        assert len({r["traceID"] for r in rows}) == 1
        assert any(r["operationName"].startswith("invoke_agent") for r in rows)
    if target["format"] == 0:  # time series: a "time" column first
        assert next(iter(rows[0])) == "time"


def test_dashboard_and_alerts_are_generated_from_code():
    import subprocess
    import sys

    before = DASHBOARD.read_text(), ALERTS.read_text()
    subprocess.run([sys.executable, str(ROOT / "grafana" / "build_dashboard.py")], check=True, capture_output=True)
    after = DASHBOARD.read_text(), ALERTS.read_text()
    assert after == before, "run `python grafana/build_dashboard.py` and commit the result"


def alert_queries():
    import yaml

    for rule in yaml.safe_load(ALERTS.read_text())["groups"][0]["rules"]:
        (query_model,) = [d["model"] for d in rule["data"] if d["datasourceUid"] != "__expr__"]
        yield pytest.param(query_model["rawSql"], id=rule["title"])


@pytest.mark.parametrize("sql", list(alert_queries()))
def test_every_alert_query_returns_a_time_series(ch, sql):
    now = datetime.now(UTC)
    rows = query(ch, expand_grafana_macros(sql, now - timedelta(hours=1), now + timedelta(hours=1)))
    assert rows and next(iter(rows[0])) == "time" and len(rows[0]) == 2


def test_data_quality_checks_catch_what_went_wrong(ch, stored):
    spans = stored
    now = datetime.now(UTC)

    def stat(title):
        (panel,) = [p for p in json.loads(DASHBOARD.read_text())["panels"] if p["title"] == title]
        sql = expand_grafana_macros(panel["targets"][0]["rawSql"], now - timedelta(hours=1), now + timedelta(hours=1))
        return float(next(iter(query(ch, sql)[0].values())))

    present = {s.context.span_id for s in spans}
    orphans = sum(1 for s in spans if s.parent and s.parent.span_id not in present)
    detached = {
        s.attributes[sc.GEN_AI_CONVERSATION_ID]
        for s in spans
        if s.name == "invoke_agent log-analyst" and s.parent is None
    }
    assert orphans >= 2 and detached  # the replay contains both failure modes
    assert stat("Spans orphelins") == orphans
    assert stat("Conversations fragmentées") == len(detached)
    assert stat("Appels LLM sans tokens") == 0
    assert stat("Spans GenAI sans agent") == 0
    assert stat("Attributs supprimés") == 0  # every attribute the agent emits is allowed
    assert stat("Durées négatives") == 0


def documented_queries():
    for doc in (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))):
        for index, block in enumerate(re.findall(r"```sql\n(.*?)```", doc.read_text(), flags=re.S)):
            yield pytest.param(block.strip().rstrip(";"), id=f"{doc.name}#{index}")


@pytest.mark.parametrize("sql", list(documented_queries()))
def test_documented_queries_run(ch, sql):
    if sql.upper().startswith("INSERT"):
        pytest.skip("write example")
    rows = query(ch, sql)
    if "suspicious_spans" in sql:
        assert rows == [{"suspicious_spans": 0}] or int(rows[0]["suspicious_spans"]) == 0
