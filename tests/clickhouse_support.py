"""Helpers to replay agent spans into an embedded ClickHouse (chDB).

`span_to_row` mirrors what the collector's ClickHouse exporter writes, and
`redact` mirrors the collector's redaction processor, using the regexes from
otel-collector/config.yaml.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from opentelemetry.trace import SpanKind, StatusCode

ROOT = Path(__file__).resolve().parents[1]
INITDB = ROOT / "clickhouse" / "initdb"
COLLECTOR_CONFIG = ROOT / "otel-collector" / "config.yaml"

KIND = {
    SpanKind.INTERNAL: "Internal",
    SpanKind.SERVER: "Server",
    SpanKind.CLIENT: "Client",
    SpanKind.PRODUCER: "Producer",
    SpanKind.CONSUMER: "Consumer",
}
STATUS = {StatusCode.UNSET: "Unset", StatusCode.OK: "Ok", StatusCode.ERROR: "Error"}


def sql_statements(path: Path) -> list[str]:
    """Split an init script into statements (full-line comments removed)."""
    text = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("--"))
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


def as_string(value) -> str:
    """pcommon.Value.AsString(): what the exporter stores in attribute maps."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), separators=(",", ":"))
    return str(value)


def timestamp(ns: int) -> str:
    seconds, nanos = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


class Redaction:
    """Mirror of the collector's redaction/genai processor, from otel-collector/config.yaml."""

    def __init__(self) -> None:
        config = yaml.safe_load(COLLECTOR_CONFIG.read_text())["processors"]["redaction/genai"]
        assert config["allow_all_keys"] is False
        self.allowed = set(config["allowed_keys"])
        self.patterns = [re.compile(p) for p in config["blocked_values"]]

    def __call__(self, attributes: dict) -> dict[str, object]:
        kept: dict[str, object] = {}
        redacted = masked = 0
        for key, value in attributes.items():
            if key not in self.allowed:
                redacted += 1
                continue
            text = as_string(value)
            new = text
            for pattern in self.patterns:
                new = pattern.sub("****", new)
            if new != text:
                masked += 1
            kept[key] = new
        allowed = len(kept)
        # summary: info -- each count is only added when non-zero
        for name, count in (("redacted", redacted), ("masked", masked), ("allowed", allowed)):
            if count:
                kept[f"redaction.{name}.count"] = str(count)
        return kept


def span_to_row(span, redaction: Redaction) -> dict:
    resource = redaction(dict(span.resource.attributes))
    return {
        "Timestamp": timestamp(span.start_time),
        "TraceId": format(span.context.trace_id, "032x"),
        "SpanId": format(span.context.span_id, "016x"),
        "ParentSpanId": format(span.parent.span_id, "016x") if span.parent else "",
        "TraceState": "",
        "SpanName": span.name,
        "SpanKind": KIND[span.kind],
        "ServiceName": resource.get("service.name", ""),
        "ResourceAttributes": resource,
        "ScopeName": span.instrumentation_scope.name if span.instrumentation_scope else "",
        "ScopeVersion": (span.instrumentation_scope.version or "") if span.instrumentation_scope else "",
        "SpanAttributes": redaction(dict(span.attributes)),
        "Duration": span.end_time - span.start_time,
        "StatusCode": STATUS[span.status.status_code],
        "StatusMessage": span.status.description or "",
        "Events.Timestamp": [timestamp(e.timestamp) for e in span.events],
        "Events.Name": [e.name for e in span.events],
        "Events.Attributes": [redaction(dict(e.attributes)) for e in span.events],
        "Links.TraceId": [],
        "Links.SpanId": [],
        "Links.TraceState": [],
        "Links.Attributes": [],
    }


def log_to_row(record, redaction: Redaction) -> dict:
    """What the exporter writes into otel_logs for one log record."""
    log = record.log_record
    resource = redaction(dict(record.resource.attributes))
    return {
        "Timestamp": timestamp(log.timestamp or log.observed_timestamp),
        "TraceId": format(log.trace_id or 0, "032x") if log.trace_id else "",
        "SpanId": format(log.span_id or 0, "016x") if log.span_id else "",
        "TraceFlags": int(log.trace_flags or 0),
        "SeverityText": log.severity_text or "",
        "SeverityNumber": log.severity_number.value if log.severity_number else 0,
        "ServiceName": resource.get("service.name", ""),
        "Body": as_string(log.body) if log.body is not None else "",
        "ResourceSchemaUrl": "",
        "ResourceAttributes": resource,
        "ScopeSchemaUrl": "",
        "ScopeName": record.instrumentation_scope.name if record.instrumentation_scope else "",
        "ScopeVersion": "",
        "ScopeAttributes": {},
        "LogAttributes": redaction(dict(log.attributes or {})),
        "EventName": log.event_name or "",
    }


def insert_rows(session, table: str, rows: list[dict]) -> None:
    if rows:
        payload = "\n".join(json.dumps(row) for row in rows)
        session.query(
            f"INSERT INTO {table} SETTINGS date_time_input_format = 'best_effort' FORMAT JSONEachRow\n{payload}"
        )


def new_session(path):
    """An embedded ClickHouse with the schema of clickhouse/initdb/ (one per process at a time)."""
    from chdb import session as chdb_session

    session = chdb_session.Session(str(path))
    for script in sorted(INITDB.glob("*.sql")):
        for statement in sql_statements(script):
            session.query(statement)
    return session


def store(session, spans, redaction: Redaction, raw_trace_ids: set[int] | None = None) -> None:
    """Write spans as the collector does: every span to the stream, which feeds the views,
    and to the raw table either all of them or only the traces kept by tail sampling."""
    rows = [span_to_row(span, redaction) for span in spans]
    insert_rows(session, "otel.otel_spans_stream", rows)
    if raw_trace_ids is not None:
        rows = [row for row, span in zip(rows, spans, strict=True) if span.context.trace_id in raw_trace_ids]
    insert_rows(session, "otel.otel_traces", rows)


def query(session, sql: str) -> list[dict]:
    result = session.query(sql, "JSONEachRow")
    text = result.bytes().decode() if hasattr(result, "bytes") else str(result)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def kept_by_tail_sampling(spans, baseline_percent: int = 10) -> set[int]:
    """The trace ids otel-collector/gateway.yaml would keep: same policies and thresholds.
    The baseline is approximated by trace id modulo 100, the collector hashes the id."""
    keep: set[int] = set()
    by_trace: dict[int, list] = {}
    for span in spans:
        by_trace.setdefault(span.context.trace_id, []).append(span)
    for trace_id, trace_spans in by_trace.items():
        interesting = any(
            s.status.status_code == StatusCode.ERROR
            or s.attributes.get("sonde.tool.blocked") is True
            or int(s.attributes.get("sonde.llm.retries", 0)) >= 1
            or int(s.attributes.get("gen_ai.usage.input_tokens", 0)) >= 12000
            or s.attributes.get("sonde.eval.sampled") is True
            for s in trace_spans
        )
        if interesting or trace_id % 100 < baseline_percent:
            keep.add(trace_id)
    return keep


def expand_grafana_macros(sql: str, start: datetime, end: datetime, interval_s: int = 60) -> str:
    """Same expansions as the Grafana ClickHouse plugin (pkg/macros)."""
    t0, t1 = int(start.timestamp()), int(end.timestamp())
    sql = re.sub(
        r"\$__timeFilter\(([^)]+)\)",
        lambda m: f"{m[1]} >= toDateTime({t0}) AND {m[1]} <= toDateTime({t1})",
        sql,
    )
    sql = re.sub(
        r"\$__timeInterval\(([^)]+)\)",
        lambda m: f"toStartOfInterval(toDateTime({m[1]}), INTERVAL {interval_s} second)",
        sql,
    )
    sql = sql.replace("$__fromTime", f"toDateTime({t0})").replace("$__toTime", f"toDateTime({t1})")
    return sql.replace("${trace_id:sqlstring}", "''")
