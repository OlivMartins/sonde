-- Answer quality, judged asynchronously (agent/sonde_agent/evaluator.py).
--
-- The evaluator emits the GenAI semantic-convention event
-- gen_ai.evaluation.result as a log record parented to the evaluated span.
-- This view turns those events into one row per evaluated run.

CREATE TABLE IF NOT EXISTS otel.genai_evaluations
(
    Timestamp DateTime64(9) CODEC(Delta, ZSTD(1)),
    TraceId String CODEC(ZSTD(1)),
    SpanId String CODEC(ZSTD(1)),
    EvaluationName LowCardinality(String),
    Score Float64,
    Label LowCardinality(String),
    Explanation String CODEC(ZSTD(1)),
    JudgeModel LowCardinality(String)
)
-- An evaluation can be emitted twice (evaluator restart): the latest one wins.
ENGINE = ReplacingMergeTree(Timestamp)
PARTITION BY toYYYYMM(Timestamp)
ORDER BY (TraceId, SpanId, EvaluationName)
TTL toDateTime(Timestamp) + INTERVAL 400 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS otel.genai_evaluations_mv
TO otel.genai_evaluations
AS SELECT
    Timestamp,
    TraceId,
    SpanId,
    LogAttributes['gen_ai.evaluation.name'] AS EvaluationName,
    toFloat64OrZero(LogAttributes['gen_ai.evaluation.score.value']) AS Score,
    LogAttributes['gen_ai.evaluation.score.label'] AS Label,
    LogAttributes['gen_ai.evaluation.explanation'] AS Explanation,
    LogAttributes['sonde.evaluation.judge_model'] AS JudgeModel
FROM otel.otel_logs
WHERE EventName = 'gen_ai.evaluation.result';

-- Latest relevance score per evaluated run.
CREATE VIEW IF NOT EXISTS otel.genai_run_scores
AS SELECT
    TraceId,
    argMax(Score, Timestamp) AS Score,
    argMax(Label, Timestamp) AS Label,
    argMax(Explanation, Timestamp) AS Explanation,
    max(Timestamp) AS EvaluatedAt
FROM otel.genai_evaluations
WHERE EvaluationName = 'relevance'
GROUP BY TraceId;
