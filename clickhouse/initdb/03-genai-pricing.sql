-- Model pricing, versioned in time.
--
-- Cost is computed at query time with an ASOF JOIN: each call or minute is
-- matched with the price that was in effect at that moment. A price change is
-- one INSERT; history stays correct and nothing has to be backfilled.
--
-- Prices below are illustrative, for the simulated models. Add your models:
--   INSERT INTO otel.genai_model_pricing VALUES ('my-model', '2026-01-01 00:00:00', 1.0, 0.25, 4.0);

CREATE TABLE IF NOT EXISTS otel.genai_model_pricing
(
    Model LowCardinality(String),
    EffectiveFrom DateTime,
    InputUsdPerMTok Float64,
    CachedInputUsdPerMTok Float64,
    OutputUsdPerMTok Float64
)
ENGINE = ReplacingMergeTree
ORDER BY (Model, EffectiveFrom);

INSERT INTO otel.genai_model_pricing VALUES
    ('sim-large', '2026-01-01 00:00:00', 2.50, 0.25, 10.00),
    ('sim-mini', '2026-01-01 00:00:00', 0.40, 0.04, 1.60);

-- Model calls with their cost.
CREATE VIEW IF NOT EXISTS otel.genai_llm_calls_priced
AS SELECT
    c.*,
    (
        (c.InputTokens - c.CacheReadTokens) * p.InputUsdPerMTok
        + c.CacheReadTokens * p.CachedInputUsdPerMTok
        + c.OutputTokens * p.OutputUsdPerMTok
    ) / 1e6 AS CostUsd,
    CAST(p.Model != '', 'UInt8') AS Priced  -- plain UInt8: the server rejects LowCardinality(UInt8) columns
FROM
(
    SELECT *, toDateTime(Timestamp) AS CallTime
    FROM otel.genai_llm_calls
) AS c
ASOF LEFT JOIN otel.genai_model_pricing AS p
    ON c.RequestModel = p.Model AND c.CallTime >= p.EffectiveFrom;

-- Per-minute cost, for long time ranges (reads the rollup, not the calls).
CREATE VIEW IF NOT EXISTS otel.genai_cost_1m
AS SELECT
    u.Minute AS Minute,
    u.AgentName AS AgentName,
    u.AgentVersion AS AgentVersion,
    u.RequestModel AS RequestModel,
    u.Calls AS Calls,
    u.InputTokens AS InputTokens,
    u.OutputTokens AS OutputTokens,
    u.CacheReadTokens AS CacheReadTokens,
    (
        (u.InputTokens - u.CacheReadTokens) * p.InputUsdPerMTok
        + u.CacheReadTokens * p.CachedInputUsdPerMTok
        + u.OutputTokens * p.OutputUsdPerMTok
    ) / 1e6 AS CostUsd
FROM
(
    SELECT
        Minute,
        AgentName,
        AgentVersion,
        RequestModel,
        sum(Calls) AS Calls,
        sum(InputTokens) AS InputTokens,
        sum(OutputTokens) AS OutputTokens,
        sum(CacheReadTokens) AS CacheReadTokens
    FROM otel.genai_usage_1m
    GROUP BY Minute, AgentName, AgentVersion, RequestModel
) AS u
ASOF LEFT JOIN otel.genai_model_pricing AS p
    ON u.RequestModel = p.Model AND u.Minute >= p.EffectiveFrom;
