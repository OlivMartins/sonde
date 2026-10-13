"""A client clock that goes back must not turn a latency into centuries.

Span timestamps come from the wall clock of the emitting process. When it is
stepped back during a span, the span ends before it starts, and the exporter
stores the negative duration in a UInt64 column, where it wraps to ~584 years.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from clickhouse_support import ROOT, Redaction, expand_grafana_macros, insert_rows, new_session, query, span_to_row
from sonde_agent import semconv as sc

pytest.importorskip("chdb")

DASHBOARD = ROOT / "grafana" / "dashboards" / "sonde-agents.json"
WRAPPED = 2**64 - 100_000_000  # what lands in Duration for a span ending 100 ms before it starts


@pytest.fixture(scope="module")
def skewed(replayed_spans, tmp_path_factory):
    session = new_session(tmp_path_factory.mktemp("chdb-clock"))
    chats = [s for s in replayed_spans if s.attributes.get(sc.GEN_AI_OPERATION_NAME) == sc.OP_CHAT][:20]
    rows = [span_to_row(span, Redaction()) for span in chats]
    rows[0]["Duration"] = WRAPPED
    for table in ("otel.otel_spans_stream", "otel.otel_traces"):
        insert_rows(session, table, rows)
    yield session, rows[0]
    session.close()


def panel_sql(title: str, trace_id: str = "") -> str:
    (panel,) = [p for p in json.loads(DASHBOARD.read_text())["panels"] if p["title"] == title]
    now = datetime.now(UTC)
    sql = panel["targets"][0]["rawSql"].replace("${trace_id:sqlstring}", f"'{trace_id}'")
    return expand_grafana_macros(sql, now - timedelta(hours=1), now + timedelta(hours=1))


def test_negative_duration_counts_as_zero_and_is_reported(skewed):
    ch, row = skewed
    calls = query(ch, "SELECT SpanId, DurationMs FROM otel.genai_llm_calls")
    durations = {c["SpanId"]: c["DurationMs"] for c in calls}
    assert durations.pop(row["SpanId"]) == 0
    assert all(0 < d < 60_000 for d in durations.values())
    assert query(ch, "SELECT sum(NegativeDurations) AS n FROM otel.span_quality_1m")[0]["n"] == 1
    assert next(iter(query(ch, panel_sql("Durées négatives"))[0].values())) == 1


def test_latency_and_trace_panels_stay_in_milliseconds(skewed):
    ch, row = skewed
    for point in query(ch, panel_sql("Latence du LLM par modèle (p50 / p95)")):
        assert 0 <= point["p50"] <= point["p95"] < 60_000
    spans = query(ch, panel_sql("Trace (dernière exécution gardée, ou celle que vous avez cliquée)", row["TraceId"]))
    assert spans and all(0 <= s["duration"] < 60_000 for s in spans)
