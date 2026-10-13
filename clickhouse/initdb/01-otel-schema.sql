-- Raw OpenTelemetry spans.
--
-- Column names and types match the INSERT statement of the ClickHouse
-- exporter (opentelemetry-collector-contrib 0.162). The exporter runs with
-- create_schema: false, so the schema is owned and versioned here rather
-- than created implicitly by whichever collector starts first.
--
-- Two destinations for the same spans (see otel-collector/config.yaml):
--   otel_spans_stream  every span, Null engine: nothing is stored, but the
--                      materialized views of 02-genai-model.sql see 100% of
--                      the traffic, so aggregates and costs stay exact.
--   otel_traces        the spans kept for drill-down, possibly tail-sampled.
--
-- Retention tiers: raw spans 3 days, typed GenAI tables 30 days,
-- per-minute rollups 400 days (see 02-genai-model.sql).

CREATE DATABASE IF NOT EXISTS otel;

CREATE TABLE IF NOT EXISTS otel.otel_traces
(
    Timestamp DateTime64(9) CODEC(Delta, ZSTD(1)),
    TraceId String CODEC(ZSTD(1)),
    SpanId String CODEC(ZSTD(1)),
    ParentSpanId String CODEC(ZSTD(1)),
    TraceState String CODEC(ZSTD(1)),
    SpanName LowCardinality(String) CODEC(ZSTD(1)),
    SpanKind LowCardinality(String) CODEC(ZSTD(1)),
    ServiceName LowCardinality(String) CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeName String CODEC(ZSTD(1)),
    ScopeVersion String CODEC(ZSTD(1)),
    SpanAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    Duration UInt64 CODEC(ZSTD(1)),
    StatusCode LowCardinality(String) CODEC(ZSTD(1)),
    StatusMessage String CODEC(ZSTD(1)),
    Events Nested
    (
        Timestamp DateTime64(9),
        Name LowCardinality(String),
        Attributes Map(LowCardinality(String), String)
    ) CODEC(ZSTD(1)),
    Links Nested
    (
        TraceId String,
        SpanId String,
        TraceState String,
        Attributes Map(LowCardinality(String), String)
    ) CODEC(ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_res_attr_key mapKeys(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_span_attr_key mapKeys(SpanAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_span_attr_value mapValues(SpanAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_duration Duration TYPE minmax GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
-- GenAI span names embed the model / tool / agent name ("chat sim-large",
-- "execute_tool query_logs"), so this key also clusters spans by model and tool.
ORDER BY (ServiceName, SpanName, toDateTime(Timestamp))
TTL toDateTime(Timestamp) + INTERVAL 3 DAY
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

-- Trace id -> time range lookup, used by the Grafana ClickHouse plugin to
-- fetch a whole trace without scanning every partition.
CREATE TABLE IF NOT EXISTS otel.otel_traces_trace_id_ts
(
    TraceId String CODEC(ZSTD(1)),
    Start DateTime CODEC(Delta, ZSTD(1)),
    End DateTime CODEC(Delta, ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(Start)
ORDER BY (TraceId, Start)
TTL Start + INTERVAL 3 DAY
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS otel.otel_traces_trace_id_ts_mv
TO otel.otel_traces_trace_id_ts
AS SELECT
    TraceId,
    min(Timestamp) AS Start,
    max(Timestamp) AS End
FROM otel.otel_traces
WHERE TraceId != ''
GROUP BY TraceId;

-- Ingestion stream: same columns as otel_traces, nothing stored. Inserts only
-- feed the materialized views. Sampling the raw table never skews the numbers.
CREATE TABLE IF NOT EXISTS otel.otel_spans_stream AS otel.otel_traces ENGINE = Null;

-- Log records, column-compatible with the exporter (EventName included).
-- Used for events such as gen_ai.evaluation.result.
CREATE TABLE IF NOT EXISTS otel.otel_logs
(
    Timestamp DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TimestampTime DateTime DEFAULT toDateTime(Timestamp),
    TraceId String CODEC(ZSTD(1)),
    SpanId String CODEC(ZSTD(1)),
    TraceFlags UInt8,
    SeverityText LowCardinality(String) CODEC(ZSTD(1)),
    SeverityNumber UInt8,
    ServiceName LowCardinality(String) CODEC(ZSTD(1)),
    Body String CODEC(ZSTD(1)),
    ResourceSchemaUrl LowCardinality(String) CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeSchemaUrl LowCardinality(String) CODEC(ZSTD(1)),
    ScopeName String CODEC(ZSTD(1)),
    ScopeVersion LowCardinality(String) CODEC(ZSTD(1)),
    ScopeAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    LogAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    EventName String CODEC(ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_log_attr_key mapKeys(LogAttributes) TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (toStartOfFiveMinutes(Timestamp), ServiceName, Timestamp)
TTL toDateTime(Timestamp) + INTERVAL 30 DAY
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
