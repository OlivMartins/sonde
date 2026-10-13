-- GenAI data model, derived from raw spans by materialized views.
--
-- Traces are the single source of truth. Instead of emitting extra metrics
-- from the application, ClickHouse turns spans into typed tables at insert
-- time, then into per-minute rollups. Dashboards read the small tables; the
-- raw spans stay available for drill-down.
--
-- The views read otel_spans_stream (100% of spans, nothing stored), not
-- otel_traces, which may be tail-sampled.
--
--   otel_spans_stream ──MV──▶ genai_agent_runs   (one row per agent run)
--                     ├─MV──▶ genai_llm_calls    (one row per model call) ──MV──▶ genai_usage_1m
--                     └─MV──▶ genai_tool_calls   (one row per tool call)
--
-- Durations do not trust client clocks. Span timestamps come from the wall
-- clock of the emitting process; if it goes back during a span (NTP step,
-- resume from sleep, Docker Desktop resyncing its VM), the span ends before
-- it starts and the exporter writes a negative duration into the UInt64
-- column, where it wraps to about 584 years. toInt64 recovers the sign; such
-- durations count as 0 here, and as NegativeDurations in span_quality_1m.

-- One row per agent run (invoke_agent span), sub-agent runs included -----------

CREATE TABLE IF NOT EXISTS otel.genai_agent_runs
(
    Timestamp DateTime64(9) CODEC(Delta, ZSTD(1)),
    TraceId String CODEC(ZSTD(1)),
    SpanId String CODEC(ZSTD(1)),
    ServiceName LowCardinality(String),
    Environment LowCardinality(String),
    AgentName LowCardinality(String),
    AgentVersion LowCardinality(String),
    ParentAgent LowCardinality(String) COMMENT 'delegating agent for a sub-agent run, empty for a main run',
    ConversationId String CODEC(ZSTD(1)),
    TaskCategory LowCardinality(String),
    RequestModel LowCardinality(String),
    Outcome LowCardinality(String),
    ErrorType LowCardinality(String),
    Steps UInt16,
    ToolCalls UInt16,
    InputTokens UInt32,
    OutputTokens UInt32,
    CacheReadTokens UInt32,
    DurationMs Float64,
    Feedback Int8 COMMENT '+1 thumbs up, -1 thumbs down, 0 not rated'
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (AgentName, AgentVersion, Timestamp)
TTL toDateTime(Timestamp) + INTERVAL 30 DAY
SETTINGS ttl_only_drop_parts = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS otel.genai_agent_runs_mv
TO otel.genai_agent_runs
AS SELECT
    Timestamp,
    TraceId,
    SpanId,
    ServiceName,
    ResourceAttributes['deployment.environment.name'] AS Environment,
    SpanAttributes['gen_ai.agent.name'] AS AgentName,
    SpanAttributes['gen_ai.agent.version'] AS AgentVersion,
    SpanAttributes['sonde.agent.parent'] AS ParentAgent,
    SpanAttributes['gen_ai.conversation.id'] AS ConversationId,
    SpanAttributes['sonde.task.category'] AS TaskCategory,
    SpanAttributes['gen_ai.request.model'] AS RequestModel,
    SpanAttributes['sonde.agent.outcome'] AS Outcome,
    SpanAttributes['error.type'] AS ErrorType,
    toUInt16OrZero(SpanAttributes['sonde.agent.steps']) AS Steps,
    toUInt16OrZero(SpanAttributes['sonde.agent.tool_calls']) AS ToolCalls,
    toUInt32OrZero(SpanAttributes['gen_ai.usage.input_tokens']) AS InputTokens,
    toUInt32OrZero(SpanAttributes['gen_ai.usage.output_tokens']) AS OutputTokens,
    toUInt32OrZero(SpanAttributes['gen_ai.usage.cache_read.input_tokens']) AS CacheReadTokens,
    greatest(toInt64(Duration), 0) / 1e6 AS DurationMs,
    toInt8OrZero(SpanAttributes['sonde.feedback.score']) AS Feedback
FROM otel.otel_spans_stream
WHERE SpanAttributes['gen_ai.operation.name'] = 'invoke_agent';

-- One row per model call (chat span) ------------------------------------------

CREATE TABLE IF NOT EXISTS otel.genai_llm_calls
(
    Timestamp DateTime64(9) CODEC(Delta, ZSTD(1)),
    TraceId String CODEC(ZSTD(1)),
    SpanId String CODEC(ZSTD(1)),
    AgentName LowCardinality(String),
    AgentVersion LowCardinality(String),
    TaskCategory LowCardinality(String),
    Provider LowCardinality(String),
    RequestModel LowCardinality(String),
    ResponseModel LowCardinality(String),
    Step UInt16,
    InputTokens UInt32,
    OutputTokens UInt32,
    CacheReadTokens UInt32,
    DurationMs Float64,
    FinishReason LowCardinality(String),
    Retries UInt8,
    StatusCode LowCardinality(String),
    ErrorType LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (AgentName, RequestModel, Timestamp)
TTL toDateTime(Timestamp) + INTERVAL 30 DAY
SETTINGS ttl_only_drop_parts = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS otel.genai_llm_calls_mv
TO otel.genai_llm_calls
AS SELECT
    Timestamp,
    TraceId,
    SpanId,
    SpanAttributes['gen_ai.agent.name'] AS AgentName,
    SpanAttributes['gen_ai.agent.version'] AS AgentVersion,
    SpanAttributes['sonde.task.category'] AS TaskCategory,
    SpanAttributes['gen_ai.provider.name'] AS Provider,
    SpanAttributes['gen_ai.request.model'] AS RequestModel,
    SpanAttributes['gen_ai.response.model'] AS ResponseModel,
    toUInt16OrZero(SpanAttributes['sonde.agent.step']) AS Step,
    toUInt32OrZero(SpanAttributes['gen_ai.usage.input_tokens']) AS InputTokens,
    toUInt32OrZero(SpanAttributes['gen_ai.usage.output_tokens']) AS OutputTokens,
    toUInt32OrZero(SpanAttributes['gen_ai.usage.cache_read.input_tokens']) AS CacheReadTokens,
    greatest(toInt64(Duration), 0) / 1e6 AS DurationMs,
    -- string[] attributes are stored as JSON arrays: '["stop"]'
    JSONExtractString(SpanAttributes['gen_ai.response.finish_reasons'], 1) AS FinishReason,
    toUInt8OrZero(SpanAttributes['sonde.llm.retries']) AS Retries,
    StatusCode,
    SpanAttributes['error.type'] AS ErrorType
FROM otel.otel_spans_stream
WHERE SpanAttributes['gen_ai.operation.name'] = 'chat';

-- One row per tool call (execute_tool span) ------------------------------------

CREATE TABLE IF NOT EXISTS otel.genai_tool_calls
(
    Timestamp DateTime64(9) CODEC(Delta, ZSTD(1)),
    TraceId String CODEC(ZSTD(1)),
    SpanId String CODEC(ZSTD(1)),
    AgentName LowCardinality(String),
    AgentVersion LowCardinality(String),
    TaskCategory LowCardinality(String),
    ToolName LowCardinality(String),
    ToolCallId String CODEC(ZSTD(1)),
    Blocked UInt8 COMMENT '1 when the call was refused by the tool allow-list',
    DurationMs Float64,
    StatusCode LowCardinality(String),
    ErrorType LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (AgentName, ToolName, Timestamp)
TTL toDateTime(Timestamp) + INTERVAL 30 DAY
SETTINGS ttl_only_drop_parts = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS otel.genai_tool_calls_mv
TO otel.genai_tool_calls
AS SELECT
    Timestamp,
    TraceId,
    SpanId,
    SpanAttributes['gen_ai.agent.name'] AS AgentName,
    SpanAttributes['gen_ai.agent.version'] AS AgentVersion,
    SpanAttributes['sonde.task.category'] AS TaskCategory,
    SpanAttributes['gen_ai.tool.name'] AS ToolName,
    SpanAttributes['gen_ai.tool.call.id'] AS ToolCallId,
    SpanAttributes['sonde.tool.blocked'] = 'true' AS Blocked,
    greatest(toInt64(Duration), 0) / 1e6 AS DurationMs,
    StatusCode,
    SpanAttributes['error.type'] AS ErrorType
FROM otel.otel_spans_stream
WHERE SpanAttributes['gen_ai.operation.name'] = 'execute_tool';

-- Per-minute rollup of model calls, kept 400 days -------------------------------

CREATE TABLE IF NOT EXISTS otel.genai_usage_1m
(
    Minute DateTime,
    AgentName LowCardinality(String),
    AgentVersion LowCardinality(String),
    RequestModel LowCardinality(String),
    Calls SimpleAggregateFunction(sum, UInt64),
    Errors SimpleAggregateFunction(sum, UInt64),
    Retries SimpleAggregateFunction(sum, UInt64),
    InputTokens SimpleAggregateFunction(sum, UInt64),
    OutputTokens SimpleAggregateFunction(sum, UInt64),
    CacheReadTokens SimpleAggregateFunction(sum, UInt64),
    LatencyMs AggregateFunction(quantiles(0.5, 0.95, 0.99), Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(Minute)
ORDER BY (AgentName, AgentVersion, RequestModel, Minute)
TTL Minute + INTERVAL 400 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS otel.genai_usage_1m_mv
TO otel.genai_usage_1m
AS SELECT
    toStartOfMinute(Timestamp) AS Minute,
    AgentName,
    AgentVersion,
    RequestModel,
    toUInt64(count()) AS Calls,
    toUInt64(countIf(StatusCode = 'Error')) AS Errors,
    toUInt64(sum(Retries)) AS Retries,
    toUInt64(sum(InputTokens)) AS InputTokens,
    toUInt64(sum(OutputTokens)) AS OutputTokens,
    toUInt64(sum(CacheReadTokens)) AS CacheReadTokens,
    quantilesState(0.5, 0.95, 0.99)(DurationMs) AS LatencyMs
FROM otel.genai_llm_calls
GROUP BY Minute, AgentName, AgentVersion, RequestModel;
