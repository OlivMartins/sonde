# Requêtes SQL

Des questions auxquelles ClickHouse répond directement (`make sql` ouvre un shell).
Chaque requête de cette page est exécutée par les tests (`tests/test_clickhouse_sql.py`).

## Coût par tâche réussie

Le chiffre qui tranche un test A/B : un modèle moins cher qui échoue plus souvent peut coûter plus cher par tâche résolue.

```sql
SELECT
    r.AgentVersion AS version,
    r.TaskCategory AS category,
    count() AS runs,
    countIf(r.Outcome = 'completed') AS successes,
    round(sum(c.CostUsd) / nullIf(successes, 0), 4) AS usd_per_success
FROM otel.genai_agent_runs AS r
LEFT JOIN
(
    SELECT TraceId, sum(CostUsd) AS CostUsd
    FROM otel.genai_llm_calls_priced
    GROUP BY TraceId
) AS c ON r.TraceId = c.TraceId
WHERE r.Timestamp >= now() - INTERVAL 1 DAY
GROUP BY version, category
ORDER BY usd_per_success DESC;
```

## Ce que le cache de prompt a fait économiser

```sql
SELECT
    u.RequestModel AS model,
    sum(u.CacheReadTokens) AS cached_tokens,
    round(sum(u.CacheReadTokens * (p.InputUsdPerMTok - p.CachedInputUsdPerMTok)) / 1e6, 4) AS usd_saved
FROM otel.genai_usage_1m AS u
ASOF LEFT JOIN otel.genai_model_pricing AS p
    ON u.RequestModel = p.Model AND u.Minute >= p.EffectiveFrom
WHERE u.Minute >= now() - INTERVAL 1 DAY
GROUP BY model;
```

## Où l’agent tourne en rond

```sql
SELECT
    TaskCategory AS category,
    AgentVersion AS version,
    count() AS runs,
    countIf(Outcome = 'max_steps_exceeded') AS loops,
    round(loops / runs * 100, 1) AS loop_pct,
    round(avg(ToolCalls), 1) AS avg_tool_calls
FROM otel.genai_agent_runs
WHERE Timestamp >= now() - INTERVAL 1 DAY
GROUP BY category, version
ORDER BY loop_pct DESC;
```

## Rejouer la dernière exécution en échec, étape par étape

```sql
SELECT
    Timestamp,
    SpanName,
    round(Duration / 1e6) AS ms,
    StatusCode,
    SpanAttributes['sonde.agent.step'] AS step,
    SpanAttributes['gen_ai.usage.input_tokens'] AS input_tokens,
    SpanAttributes['error.type'] AS error
FROM otel.otel_traces
WHERE TraceId = (
    SELECT argMax(TraceId, Timestamp)
    FROM otel.genai_agent_runs
    WHERE Outcome != 'completed'
)
ORDER BY Timestamp;
```

## Outils en erreur

```sql
SELECT
    ToolName AS tool,
    ErrorType AS error,
    count() AS errors,
    round(quantile(0.95)(DurationMs)) AS p95_ms
FROM otel.genai_tool_calls
WHERE StatusCode = 'Error' AND Timestamp >= now() - INTERVAL 1 DAY
GROUP BY tool, error
ORDER BY errors DESC;
```

## Appels d’outils refusés par la liste autorisée

Un modèle qui demande un outil qu’on ne lui a jamais donné hallucine, ou suit des instructions injectées.

```sql
SELECT
    ToolName AS tool,
    count() AS attempts,
    uniqExact(TraceId) AS runs,
    groupUniqArray(AgentVersion) AS versions,
    max(Timestamp) AS last_seen
FROM otel.genai_tool_calls
WHERE Blocked = 1
GROUP BY tool
ORDER BY attempts DESC;
```

## Contrôle : un secret a-t-il échappé au masquage ?

```sql
SELECT count() AS suspicious_spans
FROM otel.otel_traces
WHERE Timestamp >= now() - INTERVAL 1 HOUR
  AND arrayExists(
        v -> match(v, '(?i)(api[_-]?key|token|secret|password)=[^*\\s]') OR match(v, '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\\.'),
        mapValues(SpanAttributes)
      );
```

## Volume stocké et taux de compression

```sql
SELECT
    table,
    sum(rows) AS rows,
    formatReadableSize(sum(data_compressed_bytes)) AS compressed,
    formatReadableSize(sum(data_uncompressed_bytes)) AS uncompressed,
    round(sum(data_uncompressed_bytes) / sum(data_compressed_bytes), 1) AS ratio
FROM system.parts
WHERE database = 'otel' AND active
GROUP BY table
ORDER BY sum(data_compressed_bytes) DESC;
```

## Changer un prix

Les prix sont versionnés : le nouveau tarif s’applique à partir de `EffectiveFrom`, les coûts passés ne bougent pas.

```sql
INSERT INTO otel.genai_model_pricing VALUES ('sim-large', now(), 2.00, 0.20, 8.00);
```
