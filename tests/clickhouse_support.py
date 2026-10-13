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


def blocked_value_patterns() -> list[re.Pattern]:
    config = yaml.safe_load(COLLECTOR_CONFIG.read_text())
    return [re.compile(p) for p in config["processors"]["redaction/genai"]["blocked_values"]]


def redact(attributes: dict[str, str], patterns: list[re.Pattern]) -> dict[str, str]:
    masked = 0
    result = {}
    for key, value in attributes.items():
        new = value
        for pattern in patterns:
            new = pattern.sub("****", new)
        masked += new != value
        result[key] = new
    result["redaction.masked.count"] = str(masked)
    return result


def span_to_row(span, patterns: list[re.Pattern]) -> dict:
    attributes = redact({k: as_string(v) for k, v in span.attributes.items()}, patterns)
    return {
        "Timestamp": timestamp(span.start_time),
        "TraceId": format(span.context.trace_id, "032x"),
        "SpanId": format(span.context.span_id, "016x"),
        "ParentSpanId": format(span.parent.span_id, "016x") if span.parent else "",
        "TraceState": "",
        "SpanName": span.name,
        "SpanKind": KIND[span.kind],
        "ServiceName": span.resource.attributes.get("service.name", ""),
        "ResourceAttributes": {k: as_string(v) for k, v in span.resource.attributes.items()},
        "ScopeName": span.instrumentation_scope.name if span.instrumentation_scope else "",
        "ScopeVersion": (span.instrumentation_scope.version or "") if span.instrumentation_scope else "",
        "SpanAttributes": attributes,
        "Duration": span.end_time - span.start_time,
        "StatusCode": STATUS[span.status.status_code],
        "StatusMessage": span.status.description or "",
        "Events.Timestamp": [timestamp(e.timestamp) for e in span.events],
        "Events.Name": [e.name for e in span.events],
        "Events.Attributes": [{k: as_string(v) for k, v in e.attributes.items()} for e in span.events],
        "Links.TraceId": [],
        "Links.SpanId": [],
        "Links.TraceState": [],
        "Links.Attributes": [],
    }


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
    return sql.replace("${trace_id:sqlstring}", "''")
