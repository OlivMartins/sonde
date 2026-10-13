-- Data-quality counters per minute, fed by the stream.
--
-- Like the GenAI views, they read otel_spans_stream: the counts stay exact
-- when otel_traces is tail-sampled. Only the orphan-span check needs the raw
-- spans (it looks for each span's parent) and runs on otel_traces.

CREATE TABLE IF NOT EXISTS otel.span_quality_1m
(
    Minute DateTime CODEC(Delta, ZSTD(1)),
    ServiceName LowCardinality(String),
    Spans UInt64,
    GenAISpans UInt64 COMMENT 'chat and execute_tool spans',
    GenAISpansWithoutAgent UInt64 COMMENT 'chat and execute_tool spans with no gen_ai.agent.name',
    LegacyRenamed UInt64 COMMENT 'spans renamed by transform/genai_normalize',
    RedactedAttributes UInt64 COMMENT 'span attributes dropped by the allow-list',
    MaskedValues UInt64 COMMENT 'span attribute values masked by the redaction processor'
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(Minute)
ORDER BY (ServiceName, Minute)
TTL Minute + INTERVAL 400 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS otel.span_quality_1m_mv
TO otel.span_quality_1m
AS SELECT
    toStartOfMinute(Timestamp) AS Minute,
    ServiceName,
    count() AS Spans,
    countIf(SpanAttributes['gen_ai.operation.name'] IN ('chat', 'execute_tool')) AS GenAISpans,
    countIf(SpanAttributes['gen_ai.operation.name'] IN ('chat', 'execute_tool') AND SpanAttributes['gen_ai.agent.name'] = '') AS GenAISpansWithoutAgent,
    countIf(mapContains(SpanAttributes, 'sonde.semconv.legacy')) AS LegacyRenamed,
    sum(toUInt64OrZero(SpanAttributes['redaction.redacted.count'])) AS RedactedAttributes,
    sum(toUInt64OrZero(SpanAttributes['redaction.masked.count'])) AS MaskedValues
FROM otel.otel_spans_stream
GROUP BY Minute, ServiceName;
