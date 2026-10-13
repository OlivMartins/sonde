"""Dashboard as code: generates grafana/dashboards/sonde-agents.json.

    python grafana/build_dashboard.py

Every SQL query below is executed against ClickHouse (chDB) by
tests/test_clickhouse_sql.py, so a broken panel fails CI.
"""

from __future__ import annotations

import json
from pathlib import Path

OUTPUT = Path(__file__).parent / "dashboards" / "sonde-agents.json"
DATASOURCE = {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"}
PLUGIN_VERSION = "4.22.1"

# sqlds formats used by the ClickHouse plugin
FORMAT_TIMESERIES, FORMAT_TABLE, FORMAT_TRACE = 0, 1, 3

_next_id = iter(range(1, 1000))


def target(sql: str, fmt: int) -> dict:
    query_type = {FORMAT_TIMESERIES: "timeseries", FORMAT_TABLE: "table", FORMAT_TRACE: "traces"}[fmt]
    return {
        "refId": "A",
        "datasource": DATASOURCE,
        "editorType": "sql",
        "format": fmt,
        "queryType": query_type,
        "pluginVersion": PLUGIN_VERSION,
        "rawSql": sql.strip(),
    }


def panel(
    kind: str,
    title: str,
    sql: str,
    fmt: int,
    grid: tuple[int, int, int, int],
    *,
    description: str = "",
    unit: str | None = None,
    decimals: int | None = None,
    display_name: str | None = None,
    options: dict | None = None,
    custom: dict | None = None,
    overrides: list | None = None,
    links: list | None = None,
) -> dict:
    x, y, w, h = grid
    defaults: dict = {}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if display_name:
        defaults["displayName"] = display_name
    if custom:
        defaults["custom"] = custom
    if links:
        defaults["links"] = links
    return {
        "id": next(_next_id),
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(sql, fmt)],
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": options or {},
    }


def row(title: str, y: int) -> dict:
    return {
        "id": next(_next_id),
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


def stat(
    title: str,
    sql: str,
    x: int,
    y: int,
    *,
    unit: str,
    decimals: int | None = None,
    description: str = "",
    thresholds: list | None = None,
) -> dict:
    p = panel(
        "stat",
        title,
        sql,
        FORMAT_TABLE,
        (x, y, 4, 4),
        unit=unit,
        decimals=decimals,
        description=description,
        options={
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "value",
        },
    )
    if thresholds:
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
        p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    else:
        p["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": "text"}
    return p


TIMESERIES_BARS = {"drawStyle": "bars", "fillOpacity": 70, "lineWidth": 1, "stacking": {"mode": "normal", "group": "A"}}
TIMESERIES_LINES = {"drawStyle": "line", "fillOpacity": 10, "lineWidth": 2, "showPoints": "never"}
LEGEND = {
    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
    "tooltip": {"mode": "multi", "sort": "desc"},
}

TRACE_LINK = [
    {
        "title": "Open this trace below",
        "url": "/d/sonde-agents?${__url_time_range}&var-trace_id=${__value.raw}",
    }
]

panels: list[dict] = []

# --- Overview ------------------------------------------------------------------
panels.append(row("Overview", 0))
panels += [
    stat(
        "Agent runs",
        """
SELECT count() AS runs
FROM otel.genai_agent_runs
WHERE $__timeFilter(Timestamp)
""",
        0,
        1,
        unit="short",
    ),
    stat(
        "Task success rate",
        """
SELECT countIf(Outcome = 'completed') / count() AS success
FROM otel.genai_agent_runs
WHERE $__timeFilter(Timestamp)
""",
        4,
        1,
        unit="percentunit",
        decimals=1,
        description="Runs that ended with an answer, excluding loops (max steps) and LLM failures.",
        thresholds=[
            {"color": "red", "value": None},
            {"color": "orange", "value": 0.8},
            {"color": "green", "value": 0.9},
        ],
    ),
    stat(
        "p95 run duration",
        """
SELECT quantile(0.95)(DurationMs) / 1000 AS p95
FROM otel.genai_agent_runs
WHERE $__timeFilter(Timestamp)
""",
        8,
        1,
        unit="s",
        decimals=1,
    ),
    stat(
        "LLM spend",
        """
SELECT sum(CostUsd) AS spend
FROM otel.genai_cost_1m
WHERE $__timeFilter(Minute)
""",
        12,
        1,
        unit="currencyUSD",
        decimals=2,
        description="Computed in ClickHouse from token usage and the versioned price table (ASOF JOIN).",
    ),
    stat(
        "Prompt cache hit ratio",
        """
SELECT sum(CacheReadTokens) / sum(InputTokens) AS ratio
FROM otel.genai_usage_1m
WHERE $__timeFilter(Minute)
""",
        16,
        1,
        unit="percentunit",
        decimals=1,
        description="Share of input tokens served from the provider's prompt cache (cheaper).",
    ),
    stat(
        "Blocked tool calls",
        """
SELECT count() AS blocked
FROM otel.genai_tool_calls
WHERE Blocked = 1 AND $__timeFilter(Timestamp)
""",
        20,
        1,
        unit="short",
        description="Tool calls refused by the allow-list (hallucinated or injected tool names).",
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
    ),
]

# --- Cost & A/B test -------------------------------------------------------------
panels.append(row("Cost & A/B test", 5))
panels += [
    panel(
        "timeseries",
        "LLM spend per agent version",
        """
SELECT $__timeInterval(Minute) AS time, AgentVersion, sum(CostUsd) AS cost
FROM otel.genai_cost_1m
WHERE $__timeFilter(Minute)
GROUP BY time, AgentVersion
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (0, 6, 12, 8),
        unit="currencyUSD",
        decimals=3,
        display_name="${__field.labels.AgentVersion}",
        custom=TIMESERIES_BARS,
        options=LEGEND,
    ),
    panel(
        "table",
        "A/B test: quality vs cost per agent version",
        """
SELECT
    r.AgentVersion AS version,
    any(r.RequestModel) AS model,
    count() AS runs,
    countIf(r.Outcome = 'completed') / count() AS success,
    countIf(r.Feedback = 1) / nullIf(countIf(r.Feedback != 0), 0) AS thumbs_up,
    avg(r.Steps) AS avg_steps,
    quantile(0.95)(r.DurationMs) / 1000 AS p95_s,
    sum(c.CostUsd) / count() AS usd_per_run,
    sum(c.CostUsd) / nullIf(countIf(r.Outcome = 'completed'), 0) AS usd_per_success
FROM otel.genai_agent_runs AS r
LEFT JOIN
(
    SELECT TraceId, sum(CostUsd) AS CostUsd
    FROM otel.genai_llm_calls_priced
    WHERE $__timeFilter(Timestamp)
    GROUP BY TraceId
) AS c ON r.TraceId = c.TraceId
WHERE $__timeFilter(r.Timestamp)
GROUP BY version
ORDER BY version
""",
        FORMAT_TABLE,
        (12, 6, 12, 8),
        description="Is the cheaper v2 worth it? Cost per successful task is the number that matters.",
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {
                "matcher": {"id": "byRegexp", "options": "success|thumbs_up"},
                "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1}],
            },
            {
                "matcher": {"id": "byRegexp", "options": "usd_per_.*"},
                "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 4}],
            },
            {
                "matcher": {"id": "byRegexp", "options": "avg_steps|p95_s"},
                "properties": [{"id": "decimals", "value": 2}],
            },
        ],
    ),
    panel(
        "timeseries",
        "Tokens per interval",
        """
SELECT
    $__timeInterval(Minute) AS time,
    sum(InputTokens) - sum(CacheReadTokens) AS "input (uncached)",
    sum(CacheReadTokens) AS "input (cache read)",
    sum(OutputTokens) AS "output"
FROM otel.genai_usage_1m
WHERE $__timeFilter(Minute)
GROUP BY time
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (0, 14, 12, 8),
        unit="short",
        custom=TIMESERIES_BARS,
        options=LEGEND,
    ),
    panel(
        "barchart",
        "Context growth: average input tokens per agent step",
        """
SELECT
    toString(Step) AS step,
    round(avgIf(InputTokens, AgentVersion = 'v1')) AS v1,
    round(avgIf(InputTokens, AgentVersion = 'v2')) AS v2
FROM otel.genai_llm_calls
WHERE $__timeFilter(Timestamp)
GROUP BY Step
ORDER BY Step
""",
        FORMAT_TABLE,
        (12, 14, 12, 8),
        unit="short",
        description="Every tool result is appended to the prompt: long loops get expensive fast.",
        options={
            "xField": "step",
            "orientation": "vertical",
            "showValue": "never",
            "groupWidth": 0.7,
            "barWidth": 0.9,
            **LEGEND,
        },
    ),
]

# --- Reliability -------------------------------------------------------------------
panels.append(row("Reliability", 22))
panels += [
    panel(
        "timeseries",
        "Agent runs by outcome",
        """
SELECT $__timeInterval(Timestamp) AS time, Outcome, count() AS runs
FROM otel.genai_agent_runs
WHERE $__timeFilter(Timestamp)
GROUP BY time, Outcome
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (0, 23, 12, 8),
        unit="short",
        display_name="${__field.labels.Outcome}",
        custom=TIMESERIES_BARS,
        options=LEGEND,
        overrides=[
            {
                "matcher": {"id": "byName", "options": "completed"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}}],
            },
            {
                "matcher": {"id": "byName", "options": "max_steps_exceeded"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}],
            },
            {
                "matcher": {"id": "byName", "options": "llm_error"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}],
            },
        ],
    ),
    panel(
        "timeseries",
        "LLM latency by model (p50 / p95)",
        """
SELECT
    $__timeInterval(Minute) AS time,
    RequestModel,
    quantilesMerge(0.5, 0.95, 0.99)(LatencyMs)[1] AS p50,
    quantilesMerge(0.5, 0.95, 0.99)(LatencyMs)[2] AS p95
FROM otel.genai_usage_1m
WHERE $__timeFilter(Minute)
GROUP BY time, RequestModel
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (12, 23, 12, 8),
        unit="ms",
        display_name="${__field.labels.RequestModel} ${__field.name}",
        custom=TIMESERIES_LINES,
        options=LEGEND,
    ),
    panel(
        "table",
        "Outcomes by task category",
        """
SELECT
    TaskCategory AS category,
    count() AS runs,
    countIf(Outcome = 'completed') / count() AS success,
    countIf(Outcome = 'max_steps_exceeded') AS loops,
    avg(Steps) AS avg_steps
FROM otel.genai_agent_runs
WHERE $__timeFilter(Timestamp)
GROUP BY category
ORDER BY success
""",
        FORMAT_TABLE,
        (0, 31, 12, 8),
        description="Vague requests make the agent wander until it hits max steps.",
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {
                "matcher": {"id": "byName", "options": "success"},
                "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1}],
            },
            {"matcher": {"id": "byName", "options": "avg_steps"}, "properties": [{"id": "decimals", "value": 1}]},
        ],
    ),
    panel(
        "timeseries",
        "Rate limits: retries and failed LLM calls",
        """
SELECT $__timeInterval(Minute) AS time, sum(Retries) AS retries, sum(Errors) AS "failed calls"
FROM otel.genai_usage_1m
WHERE $__timeFilter(Minute)
GROUP BY time
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (12, 31, 12, 8),
        unit="short",
        custom=TIMESERIES_BARS,
        options=LEGEND,
        description="Try: curl -X POST localhost:8080/admin/faults -d '{\"rate_limit_rate\": 0.4}'",
    ),
]

# --- Tools & security ------------------------------------------------------------------
panels.append(row("Tools & security", 39))
panels += [
    panel(
        "table",
        "Tool calls",
        """
SELECT
    ToolName AS tool,
    count() AS calls,
    countIf(StatusCode = 'Error' AND Blocked = 0) / count() AS error_rate,
    sum(Blocked) AS blocked,
    quantile(0.95)(DurationMs) AS p95_ms,
    topKIf(1)(ErrorType, ErrorType != '')[1] AS top_error
FROM otel.genai_tool_calls
WHERE $__timeFilter(Timestamp)
GROUP BY tool
ORDER BY calls DESC
""",
        FORMAT_TABLE,
        (0, 40, 10, 8),
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {
                "matcher": {"id": "byName", "options": "error_rate"},
                "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1}],
            },
            {
                "matcher": {"id": "byName", "options": "p95_ms"},
                "properties": [{"id": "unit", "value": "ms"}, {"id": "decimals", "value": 0}],
            },
        ],
    ),
    panel(
        "timeseries",
        "Secrets and PII masked by the collector",
        """
SELECT $__timeInterval(Timestamp) AS time, sum(toUInt64OrZero(SpanAttributes['redaction.masked.count'])) AS "masked values"
FROM otel.otel_traces
WHERE $__timeFilter(Timestamp)
GROUP BY time
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (10, 40, 7, 8),
        unit="short",
        custom=TIMESERIES_BARS,
        options=LEGEND,
        description="E-mails, API keys and bearer tokens found in prompts and tool results, masked before storage.",
    ),
    panel(
        "table",
        "Blocked tool attempts",
        """
SELECT Timestamp AS time, AgentVersion AS version, ToolName AS tool, TraceId AS trace_id
FROM otel.genai_tool_calls
WHERE Blocked = 1 AND $__timeFilter(Timestamp)
ORDER BY Timestamp DESC
LIMIT 50
""",
        FORMAT_TABLE,
        (17, 40, 7, 8),
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {"matcher": {"id": "byName", "options": "trace_id"}, "properties": [{"id": "links", "value": TRACE_LINK}]}
        ],
    ),
]

# --- Investigate ------------------------------------------------------------------------
panels.append(row("Investigate", 48))
panels += [
    panel(
        "table",
        "Most expensive runs (click a trace id)",
        """
SELECT
    r.Timestamp AS time,
    r.AgentVersion AS version,
    r.TaskCategory AS category,
    r.Outcome AS outcome,
    r.Steps AS steps,
    r.InputTokens + r.OutputTokens AS tokens,
    c.CostUsd AS cost,
    r.DurationMs / 1000 AS duration,
    r.TraceId AS trace_id
FROM otel.genai_agent_runs AS r
INNER JOIN
(
    SELECT TraceId, sum(CostUsd) AS CostUsd
    FROM otel.genai_llm_calls_priced
    WHERE $__timeFilter(Timestamp)
    GROUP BY TraceId
) AS c ON r.TraceId = c.TraceId
WHERE $__timeFilter(r.Timestamp)
ORDER BY c.CostUsd DESC
LIMIT 20
""",
        FORMAT_TABLE,
        (0, 49, 24, 8),
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {"matcher": {"id": "byName", "options": "trace_id"}, "properties": [{"id": "links", "value": TRACE_LINK}]},
            {
                "matcher": {"id": "byName", "options": "cost"},
                "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 4}],
            },
            {
                "matcher": {"id": "byName", "options": "duration"},
                "properties": [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 1}],
            },
        ],
    ),
    panel(
        "traces",
        "Trace explorer (latest run, or the trace id you clicked above)",
        """
WITH
    coalesce(nullIf(${trace_id:sqlstring}, ''), (SELECT argMax(TraceId, Timestamp) FROM otel.genai_agent_runs)) AS trace_id,
    (SELECT min(Start) FROM otel.otel_traces_trace_id_ts WHERE TraceId = trace_id) AS trace_start,
    (SELECT max(End) + 1 FROM otel.otel_traces_trace_id_ts WHERE TraceId = trace_id) AS trace_end
SELECT
    TraceId AS traceID,
    SpanId AS spanID,
    ParentSpanId AS parentSpanID,
    ServiceName AS serviceName,
    SpanName AS operationName,
    multiply(toUnixTimestamp64Nano(Timestamp), 0.000001) AS startTime,
    multiply(Duration, 0.000001) AS duration,
    arrayMap(key -> map('key', key, 'value', SpanAttributes[key]), mapKeys(SpanAttributes)) AS tags,
    arrayMap(key -> map('key', key, 'value', ResourceAttributes[key]), mapKeys(ResourceAttributes)) AS serviceTags,
    if(StatusCode IN ('Error', 'STATUS_CODE_ERROR'), 2, 0) AS statusCode
FROM otel.otel_traces
WHERE TraceId = trace_id AND Timestamp >= trace_start AND Timestamp <= trace_end
ORDER BY startTime ASC
""",
        FORMAT_TRACE,
        (0, 57, 24, 16),
    ),
]

dashboard = {
    "uid": "sonde-agents",
    "title": "Sonde — AI agent observability",
    "description": "OpenTelemetry GenAI traces stored in ClickHouse: cost, quality, reliability and safety of an agent.",
    "tags": ["genai", "opentelemetry", "clickhouse", "agents"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "10s",
    "time": {"from": "now-30m", "to": "now"},
    "schemaVersion": 41,
    "version": 1,
    "annotations": {"list": []},
    "templating": {
        "list": [
            {
                "type": "textbox",
                "name": "trace_id",
                "label": "Trace ID",
                "query": "",
                "current": {"text": "", "value": ""},
                "options": [],
                "hide": 0,
            }
        ]
    },
    "panels": panels,
}

if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUTPUT} ({len(panels)} panels)")
