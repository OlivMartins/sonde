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
WHERE r.ParentAgent = '' AND r.Timestamp >= now() - INTERVAL 1 DAY
GROUP BY version, category
ORDER BY usd_per_success DESC;
```

## Coût par bonne réponse

Une tâche « réussie » s’est terminée par une réponse, pas forcément par une bonne. Le juge note un échantillon des exécutions abouties ; son taux de réussite s’applique à toutes.

```sql
SELECT
    r.AgentVersion AS version,
    countIf(r.Outcome = 'completed') AS successes,
    countIf(s.TraceId != '') AS judged,
    round(avgIf(s.Score, s.TraceId != ''), 2) AS judge_score,
    round(countIf(s.Label = 'pass') / nullIf(judged, 0), 3) AS good_rate,
    round(sum(c.CostUsd) / nullIf(successes * good_rate, 0), 4) AS usd_per_good_answer
FROM otel.genai_agent_runs AS r
LEFT JOIN
(
    SELECT TraceId, sum(CostUsd) AS CostUsd
    FROM otel.genai_llm_calls_priced
    GROUP BY TraceId
) AS c ON r.TraceId = c.TraceId
LEFT JOIN otel.genai_run_scores AS s ON r.TraceId = s.TraceId
WHERE r.ParentAgent = '' AND r.Timestamp >= now() - INTERVAL 1 DAY
GROUP BY version
ORDER BY version;
```

## Les réponses recalées par le juge, avec la question

```sql
SELECT
    s.Score AS score,
    s.Explanation AS why,
    t.SpanAttributes['gen_ai.input.messages'] AS question,
    t.SpanAttributes['gen_ai.output.messages'] AS answer,
    t.TraceId AS trace_id
FROM otel.genai_run_scores AS s
INNER JOIN otel.otel_traces AS t ON t.TraceId = s.TraceId
WHERE s.Label = 'fail'
  AND t.SpanAttributes['sonde.eval.sampled'] = 'true'
  AND t.Timestamp >= now() - INTERVAL 1 DAY
ORDER BY score
LIMIT 20;
```

## Ce que coûte le juge

```sql
SELECT
    AgentName AS agent,
    round(sum(CostUsd), 4) AS usd,
    round(usd / (SELECT sum(CostUsd) FROM otel.genai_cost_1m WHERE Minute >= now() - INTERVAL 1 DAY) * 100, 1) AS pct_of_spend
FROM otel.genai_cost_1m
WHERE Minute >= now() - INTERVAL 1 DAY
GROUP BY agent
ORDER BY usd DESC;
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
WHERE ParentAgent = '' AND Timestamp >= now() - INTERVAL 1 DAY
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

## Ce que coûtent les sous-agents

La dépense par agent : les appels d’un sous-agent portent son propre nom.

```sql
SELECT
    AgentName AS agent,
    count() AS llm_calls,
    round(sum(CostUsd), 4) AS cost_usd,
    round(sum(CostUsd) / sum(sum(CostUsd)) OVER (), 3) AS share
FROM otel.genai_llm_calls_priced
WHERE Timestamp >= now() - INTERVAL 1 DAY
GROUP BY agent
ORDER BY cost_usd DESC;
```

## Conversations fragmentées

Une conversation répartie sur plusieurs traces : un sous-agent est parti sans le contexte de son parent.

```sql
SELECT
    ConversationId AS conversation,
    uniqExact(TraceId) AS traces,
    groupUniqArray(AgentName) AS agents,
    groupUniqArrayIf(TraceId, ParentAgent != '' AND TraceId NOT IN (
        SELECT TraceId FROM otel.genai_agent_runs WHERE ParentAgent = ''
    )) AS detached_traces
FROM otel.genai_agent_runs
WHERE ConversationId != '' AND Timestamp >= now() - INTERVAL 1 HOUR
GROUP BY conversation
HAVING traces > 1
ORDER BY traces DESC;
```

## Spans orphelins

Des spans dont le parent n’est jamais arrivé. Les deux dernières minutes sont écartées : un parent n’est exporté qu’une fois terminé, après ses enfants.

```sql
SELECT ServiceName, SpanName, count() AS orphans
FROM otel.otel_traces
WHERE Timestamp >= now() - INTERVAL 1 HOUR
  AND Timestamp <= now() - INTERVAL 2 MINUTE
  AND ParentSpanId != ''
  AND (TraceId, ParentSpanId) NOT IN
  (
      SELECT TraceId, SpanId FROM otel.otel_traces WHERE Timestamp >= now() - INTERVAL 2 HOUR
  )
GROUP BY ServiceName, SpanName
ORDER BY orphans DESC;
```

## Ce que l’échantillonnage a gardé

Avec le profil d’échantillonnage, les exécutions sont toutes comptées, mais seules certaines gardent leur trace brute. Les échecs doivent être à 100 %.

```sql
SELECT
    Outcome AS outcome,
    count() AS runs,
    countIf(TraceId IN (SELECT TraceId FROM otel.otel_traces_trace_id_ts)) AS raw_kept,
    round(raw_kept / runs * 100, 1) AS kept_pct
FROM otel.genai_agent_runs
WHERE ParentAgent = '' AND Timestamp >= now() - INTERVAL 1 DAY
GROUP BY outcome
ORDER BY outcome;
```

## Attributs renommés, supprimés ou masqués, par service

Ces compteurs sont alimentés par le flux : ils restent exacts quand les spans bruts sont échantillonnés.

```sql
SELECT
    ServiceName AS service,
    sum(Spans) AS spans,
    sum(LegacyRenamed) AS renamed,
    sum(RedactedAttributes) AS attributes_dropped,
    sum(MaskedValues) AS values_masked
FROM otel.span_quality_1m
WHERE Minute >= now() - INTERVAL 1 DAY
GROUP BY service
ORDER BY spans DESC;
```

Le détail, par nom d’attribut converti :

```sql
SELECT
    SpanAttributes['sonde.semconv.legacy'] AS renamed_from,
    count() AS spans,
    sum(toUInt64OrZero(SpanAttributes['redaction.redacted.count'])) AS attributes_dropped
FROM otel.otel_traces
WHERE Timestamp >= now() - INTERVAL 1 DAY
GROUP BY renamed_from
ORDER BY spans DESC;
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
