"""End-to-end check of the ClickHouse layer, without a server.

Agent runs are replayed into chDB (embedded ClickHouse) exactly as the
collector would write them: same columns, same attribute encoding, same
redaction. Then the materialized views, the cost views and every Grafana
panel query are executed.
"""

from __future__ import annotations

import json
import random
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from clickhouse_support import (
    INITDB,
    ROOT,
    blocked_value_patterns,
    expand_grafana_macros,
    span_to_row,
    sql_statements,
)
from sonde_agent import semconv as sc
from sonde_agent import tools as tools_module
from sonde_agent.agent import Agent
from sonde_agent.llm import LLMClient
from sonde_agent.scenarios import build_variants, make_feedback_fn, pick_task
from sonde_agent.tools import ToolRegistry, build_tools

chdb_session = pytest.importorskip("chdb.session")

DASHBOARD = ROOT / "grafana" / "dashboards" / "sonde-agents.json"
RUNS = 60


@pytest.fixture(scope="module")
def spans(span_exporter, fake_llm_url):
    import fake_llm

    original = fake_llm.Faults.snapshot()
    fake_llm.Faults.update(
        {
            "latency_factor": 0,
            "retry_after_s": 0,
            "rate_limit_rate": 0.1,
            "error_rate": 0.02,
            "loop_rate": 0.75,
            "hallucinated_tool_rate": 1,
            "parallel_tool_rate": 0.3,
            "small_model_dither_rate": 0.25,
        }
    )
    fake_llm.Handler.rng.seed(42)
    span_exporter.clear()
    rng = random.Random(42)
    variants = build_variants("sre-copilot", "sim-large", "sim-mini")
    llm = LLMClient(fake_llm_url, provider="fake-llm", capture_content=True, max_retries=2, backoff_base_s=0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tools_module.time, "sleep", lambda _s: None)
        registry = ToolRegistry(build_tools(rng), capture_content=True)
        for _ in range(RUNS):
            version = rng.choice(["v1", "v2"])
            category, task = pick_task(rng)
            agent = Agent(variants[version], llm, registry, feedback_fn=make_feedback_fn(rng, version))
            agent.run(task, conversation_id=str(uuid.uuid4()), category=category)
    llm.close()
    fake_llm.Faults.update(original)
    finished = list(span_exporter.get_finished_spans())
    span_exporter.clear()
    return finished


@pytest.fixture(scope="module")
def ch(spans, tmp_path_factory):
    session = chdb_session.Session(str(tmp_path_factory.mktemp("chdb")))
    for script in sorted(INITDB.glob("*.sql")):
        for statement in sql_statements(script):
            session.query(statement)

    patterns = blocked_value_patterns()
    rows = "\n".join(json.dumps(span_to_row(span, patterns)) for span in spans)
    session.query(
        "INSERT INTO otel.otel_traces SETTINGS date_time_input_format = 'best_effort' FORMAT JSONEachRow\n" + rows
    )
    yield session
    session.close()


def query(session, sql: str) -> list[dict]:
    result = session.query(sql, "JSONEachRow")
    text = result.bytes().decode() if hasattr(result, "bytes") else str(result)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def count_operation(spans, operation: str) -> int:
    return sum(1 for s in spans if s.attributes.get(sc.GEN_AI_OPERATION_NAME) == operation)


def test_materialized_views_type_every_genai_span(ch, spans):
    assert query(ch, "SELECT count() AS n FROM otel.otel_traces")[0]["n"] == len(spans)
    assert query(ch, "SELECT count() AS n FROM otel.genai_agent_runs")[0]["n"] == RUNS
    assert query(ch, "SELECT count() AS n FROM otel.genai_llm_calls")[0]["n"] == count_operation(spans, sc.OP_CHAT)
    assert query(ch, "SELECT count() AS n FROM otel.genai_tool_calls")[0]["n"] == count_operation(
        spans, sc.OP_EXECUTE_TOOL
    )

    runs = query(ch, "SELECT * FROM otel.genai_agent_runs")
    assert {r["AgentVersion"] for r in runs} == {"v1", "v2"}
    assert {r["Outcome"] for r in runs} <= {"completed", "max_steps_exceeded", "llm_error"}
    assert all(r["AgentName"] == "sre-copilot" and r["Steps"] >= 1 for r in runs)
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


def test_dashboard_is_generated_from_code():
    import subprocess
    import sys

    before = DASHBOARD.read_text()
    subprocess.run([sys.executable, str(ROOT / "grafana" / "build_dashboard.py")], check=True, capture_output=True)
    assert DASHBOARD.read_text() == before, "run `python grafana/build_dashboard.py` and commit the result"


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
