"""Dashboard as code: generates grafana/dashboards/sonde-agents.json and the alert rules.

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
    w: int = 4,
    h: int = 4,
) -> dict:
    p = panel(
        "stat",
        title,
        sql,
        FORMAT_TABLE,
        (x, y, w, h),
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
        "title": "Ouvrir cette trace plus bas",
        "url": "/d/sonde-agents?${__url_time_range}&var-trace_id=${__value.raw}",
    }
]

panels: list[dict] = []

# Run-level panels only count main runs: sub-agent runs are part of their parent's run.
MAIN = "ParentAgent = ''"

# --- Vue d'ensemble -------------------------------------------------------------
panels.append(row("Vue d'ensemble", 0))
panels += [
    stat(
        "Exécutions de l'agent",
        f"""
SELECT count() AS runs
FROM otel.genai_agent_runs
WHERE {MAIN} AND $__timeFilter(Timestamp)
""",
        0,
        1,
        unit="short",
        description="Exécutions principales ; les sous-agents sont comptés dans l'exécution qui les a délégués.",
    ),
    stat(
        "Taux de succès",
        f"""
SELECT countIf(Outcome = 'completed') / count() AS success
FROM otel.genai_agent_runs
WHERE {MAIN} AND $__timeFilter(Timestamp)
""",
        4,
        1,
        unit="percentunit",
        decimals=1,
        description="Exécutions terminées par une réponse : hors boucles (nombre d'étapes maximal atteint) et échecs du LLM.",
        thresholds=[
            {"color": "red", "value": None},
            {"color": "orange", "value": 0.8},
            {"color": "green", "value": 0.9},
        ],
    ),
    stat(
        "Durée p95 d'une exécution",
        f"""
SELECT quantile(0.95)(DurationMs) / 1000 AS p95
FROM otel.genai_agent_runs
WHERE {MAIN} AND $__timeFilter(Timestamp)
""",
        8,
        1,
        unit="s",
        decimals=1,
    ),
    stat(
        "Dépense LLM",
        """
SELECT sum(CostUsd) AS spend
FROM otel.genai_cost_1m
WHERE $__timeFilter(Minute)
""",
        12,
        1,
        unit="currencyUSD",
        decimals=2,
        description="Calculée dans ClickHouse à partir des tokens et de la table de prix versionnée (ASOF JOIN).",
    ),
    stat(
        "Taux de cache du prompt",
        """
SELECT sum(CacheReadTokens) / sum(InputTokens) AS ratio
FROM otel.genai_usage_1m
WHERE $__timeFilter(Minute)
""",
        16,
        1,
        unit="percentunit",
        decimals=1,
        description="Part des tokens d'entrée servis par le cache du fournisseur, facturés moins cher.",
    ),
    stat(
        "Appels d'outils refusés",
        """
SELECT count() AS blocked
FROM otel.genai_tool_calls
WHERE Blocked = 1 AND $__timeFilter(Timestamp)
""",
        20,
        1,
        unit="short",
        description="Appels refusés par la liste d'outils autorisés : nom halluciné ou injecté.",
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
    ),
]

# --- Coût et test A/B ------------------------------------------------------------
panels.append(row("Coût et test A/B", 5))
panels += [
    panel(
        "timeseries",
        "Dépense LLM par agent et version",
        """
SELECT $__timeInterval(Minute) AS time, concat(AgentName, ' ', AgentVersion) AS agent, sum(CostUsd) AS cost
FROM otel.genai_cost_1m
WHERE $__timeFilter(Minute)
GROUP BY time, agent
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (0, 6, 8, 8),
        unit="currencyUSD",
        decimals=3,
        display_name="${__field.labels.agent}",
        custom=TIMESERIES_BARS,
        options=LEGEND,
    ),
    panel(
        "timeseries",
        "Tokens par intervalle",
        """
SELECT
    $__timeInterval(Minute) AS time,
    sum(InputTokens) - sum(CacheReadTokens) AS "entrée (hors cache)",
    sum(CacheReadTokens) AS "entrée (cache)",
    sum(OutputTokens) AS "sortie"
FROM otel.genai_usage_1m
WHERE $__timeFilter(Minute)
GROUP BY time
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (8, 6, 8, 8),
        unit="short",
        custom=TIMESERIES_BARS,
        options=LEGEND,
    ),
    panel(
        "barchart",
        "Croissance du contexte : tokens d'entrée moyens par étape",
        """
SELECT
    toString(Step) AS step,
    round(avgIf(InputTokens, AgentVersion = 'v1')) AS v1,
    round(avgIf(InputTokens, AgentVersion = 'v2')) AS v2
FROM otel.genai_llm_calls
WHERE AgentName = 'sre-copilot' AND $__timeFilter(Timestamp)
GROUP BY Step
ORDER BY Step
""",
        FORMAT_TABLE,
        (16, 6, 8, 8),
        unit="short",
        description="Chaque résultat d'outil s'ajoute au prompt : une boucle longue devient vite chère.",
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

panels.append(
    panel(
        "table",
        "Test A/B : qualité et coût par version",
        f"""
SELECT
    r.AgentVersion AS version,
    any(r.RequestModel) AS model,
    count() AS runs,
    countIf(r.Outcome = 'completed') / count() AS success,
    countIf(r.Feedback = 1) / nullIf(countIf(r.Feedback != 0), 0) AS thumbs_up,
    countIf(s.TraceId != '') AS judged,
    avgIf(s.Score, s.TraceId != '') AS judge_score,
    countIf(s.Label = 'pass') / nullIf(judged, 0) AS good_answers,
    avg(r.Steps) AS avg_steps,
    quantile(0.95)(r.DurationMs) / 1000 AS p95_s,
    sum(c.CostUsd) / count() AS usd_per_run,
    sum(c.CostUsd) / nullIf(countIf(r.Outcome = 'completed'), 0) AS usd_per_success,
    sum(c.CostUsd) / nullIf(countIf(r.Outcome = 'completed') * good_answers, 0) AS usd_per_good_answer
FROM otel.genai_agent_runs AS r
LEFT JOIN
(
    SELECT TraceId, sum(CostUsd) AS CostUsd
    FROM otel.genai_llm_calls_priced
    WHERE $__timeFilter(Timestamp)
    GROUP BY TraceId
) AS c ON r.TraceId = c.TraceId
LEFT JOIN otel.genai_run_scores AS s ON r.TraceId = s.TraceId
WHERE r.{MAIN} AND $__timeFilter(r.Timestamp)
GROUP BY version
ORDER BY version
""",
        FORMAT_TABLE,
        (0, 14, 24, 8),
        description=(
            "La v2, moins chère, vaut-elle le coup ? Une tâche réussie n'est pas une bonne réponse : le juge note "
            "un échantillon des exécutions abouties, et le coût par bonne réponse applique son taux de réussite à "
            "toutes. Le coût d'une exécution inclut ses sous-agents, tant que le contexte de trace est propagé."
        ),
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {
                "matcher": {"id": "byRegexp", "options": "success|thumbs_up|good_answers"},
                "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1}],
            },
            {
                "matcher": {"id": "byRegexp", "options": "usd_per_.*"},
                "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 4}],
            },
            {
                "matcher": {"id": "byRegexp", "options": "avg_steps|p95_s|judge_score"},
                "properties": [{"id": "decimals", "value": 2}],
            },
        ],
    )
)

# --- Qualité des réponses -------------------------------------------------------------
# Scores from the asynchronous judge (agent/sonde_agent/evaluator.py), stored as
# gen_ai.evaluation.result events and joined to the runs by trace id.
JUDGED_RUNS = f"""
FROM otel.genai_agent_runs AS r
INNER JOIN otel.genai_run_scores AS s ON r.TraceId = s.TraceId
WHERE r.{MAIN} AND $__timeFilter(r.Timestamp)"""

panels.append(row("Qualité des réponses", 22))
panels += [
    panel(
        "timeseries",
        "Note du juge par version (1 à 5)",
        f"""
SELECT $__timeInterval(r.Timestamp) AS time, r.AgentVersion AS version, avg(s.Score) AS score{JUDGED_RUNS}
GROUP BY time, version
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (0, 23, 9, 8),
        decimals=2,
        display_name="${__field.labels.version}",
        custom={**TIMESERIES_LINES, "axisSoftMin": 1, "axisSoftMax": 5},
        options=LEGEND,
        description=(
            "Pertinence des réponses notée par un LLM juge, hors du chemin de l'agent. Une version moins chère "
            "peut garder son taux de succès et répondre moins bien : seul ce panneau le montre."
        ),
    ),
    stat(
        "Réponses jugées",
        """
SELECT uniqExact(TraceId) AS judged
FROM otel.genai_evaluations
WHERE $__timeFilter(Timestamp)
""",
        9,
        23,
        unit="short",
        w=3,
        description="Exécutions abouties échantillonnées (SONDE_EVAL_SAMPLE_RATE), puis notées par l'évaluateur.",
    ),
    stat(
        "Coût du juge",
        """
SELECT sum(CostUsd) AS spend
FROM otel.genai_cost_1m
WHERE AgentName = 'sonde-evaluator' AND $__timeFilter(Minute)
""",
        9,
        27,
        unit="currencyUSD",
        decimals=3,
        w=3,
        description="L'évaluation consomme aussi des tokens : ses appels sont tracés et chiffrés comme ceux de l'agent.",
    ),
    panel(
        "table",
        "Réponses les moins bien notées (cliquez un identifiant de trace)",
        f"""
SELECT
    r.Timestamp AS time,
    r.AgentVersion AS version,
    r.TaskCategory AS category,
    s.Score AS score,
    s.Label AS label,
    s.Explanation AS explanation,
    r.TraceId AS trace_id{JUDGED_RUNS}
ORDER BY score ASC, time DESC
LIMIT 20
""",
        FORMAT_TABLE,
        (12, 23, 12, 8),
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {"matcher": {"id": "byName", "options": "trace_id"}, "properties": [{"id": "links", "value": TRACE_LINK}]},
            {"matcher": {"id": "byName", "options": "score"}, "properties": [{"id": "decimals", "value": 1}]},
        ],
    ),
]

# --- Fiabilité -------------------------------------------------------------------
panels.append(row("Fiabilité", 31))
panels += [
    panel(
        "timeseries",
        "Exécutions par issue",
        f"""
SELECT $__timeInterval(Timestamp) AS time, Outcome, count() AS runs
FROM otel.genai_agent_runs
WHERE {MAIN} AND $__timeFilter(Timestamp)
GROUP BY time, Outcome
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (0, 32, 12, 8),
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
        "Latence du LLM par modèle (p50 / p95)",
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
        (12, 32, 12, 8),
        unit="ms",
        display_name="${__field.labels.RequestModel} ${__field.name}",
        custom=TIMESERIES_LINES,
        options=LEGEND,
    ),
    panel(
        "table",
        "Issues par catégorie de tâche",
        f"""
SELECT
    TaskCategory AS category,
    count() AS runs,
    countIf(Outcome = 'completed') / count() AS success,
    countIf(Outcome = 'max_steps_exceeded') AS loops,
    avg(Steps) AS avg_steps
FROM otel.genai_agent_runs
WHERE {MAIN} AND $__timeFilter(Timestamp)
GROUP BY category
ORDER BY success
""",
        FORMAT_TABLE,
        (0, 40, 12, 8),
        description="Les demandes vagues font tourner l'agent en rond jusqu'au nombre d'étapes maximal.",
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
        "Limites de débit : tentatives et appels LLM en échec",
        """
SELECT $__timeInterval(Minute) AS time, sum(Retries) AS "nouvelles tentatives", sum(Errors) AS "appels en échec"
FROM otel.genai_usage_1m
WHERE $__timeFilter(Minute)
GROUP BY time
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (12, 40, 12, 8),
        unit="short",
        custom=TIMESERIES_BARS,
        options=LEGEND,
        description="Essayez : curl -X POST localhost:8080/admin/faults -d '{\"rate_limit_rate\": 0.4}'",
    ),
]

# --- Outils et sécurité -------------------------------------------------------------
panels.append(row("Outils et sécurité", 48))
panels += [
    panel(
        "table",
        "Appels d'outils",
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
        (0, 49, 10, 8),
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
        "Secrets et données personnelles masqués",
        """
SELECT $__timeInterval(Minute) AS time, sum(MaskedValues) AS "valeurs masquées"
FROM otel.span_quality_1m
WHERE $__timeFilter(Minute)
GROUP BY time
ORDER BY time
""",
        FORMAT_TIMESERIES,
        (10, 49, 7, 8),
        unit="short",
        custom=TIMESERIES_BARS,
        options=LEGEND,
        description=(
            "E-mails, clés d'API et jetons trouvés dans les prompts et résultats d'outils, masqués avant stockage. "
            "Les regex ne voient que des formats connus : voir les limites dans docs/ARCHITECTURE.md."
        ),
    ),
    panel(
        "table",
        "Tentatives d'outils refusées",
        """
SELECT Timestamp AS time, AgentVersion AS version, ToolName AS tool, TraceId AS trace_id
FROM otel.genai_tool_calls
WHERE Blocked = 1 AND $__timeFilter(Timestamp)
ORDER BY Timestamp DESC
LIMIT 50
""",
        FORMAT_TABLE,
        (17, 49, 7, 8),
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {"matcher": {"id": "byName", "options": "trace_id"}, "properties": [{"id": "links", "value": TRACE_LINK}]}
        ],
    ),
]

# --- Qualité des données ------------------------------------------------------------
# A wrong dashboard is worse than a broken one: these panels catch silent failures.
ORPHAN_FILTER = """
WHERE $__timeFilter(Timestamp)
  AND Timestamp <= $__toTime - INTERVAL 2 MINUTE
  AND ParentSpanId != ''
  AND (TraceId, ParentSpanId) NOT IN
  (
      SELECT TraceId, SpanId
      FROM otel.otel_traces
      WHERE Timestamp >= $__fromTime - INTERVAL 1 HOUR AND Timestamp <= $__toTime
  )"""
FRAGMENTED_CONVERSATIONS = """
    SELECT ConversationId, min(Timestamp) AS first_seen
    FROM otel.genai_agent_runs
    WHERE ConversationId != '' AND $__timeFilter(Timestamp)
    GROUP BY ConversationId
    HAVING uniqExact(TraceId) > 1"""
ORPHANS = f"""
SELECT count() AS orphans
FROM otel.otel_traces{ORPHAN_FILTER}
"""
FRAGMENTED = f"""
SELECT count() AS conversations
FROM
({FRAGMENTED_CONVERSATIONS}
)
"""
ZERO_GOOD = [{"color": "green", "value": None}, {"color": "orange", "value": 1}]

panels.append(row("Qualité des données", 57))
panels += [
    stat(
        "Appels LLM sans tokens",
        """
SELECT countIf(InputTokens = 0 AND StatusCode != 'Error') / count() AS ratio
FROM otel.genai_llm_calls
WHERE $__timeFilter(Timestamp)
""",
        0,
        58,
        w=3,
        unit="percentunit",
        decimals=1,
        description=(
            "Appels réussis sans usage de tokens : signe d'un attribut renommé ou d'une instrumentation "
            "qui ne suit pas les conventions. Le coût affiché est alors faux, sans aucune erreur."
        ),
        thresholds=[
            {"color": "green", "value": None},
            {"color": "orange", "value": 0.01},
            {"color": "red", "value": 0.05},
        ],
    ),
    stat(
        "Spans GenAI sans agent",
        """
SELECT sum(GenAISpansWithoutAgent) / sum(GenAISpans) AS ratio
FROM otel.span_quality_1m
WHERE $__timeFilter(Minute)
""",
        3,
        58,
        w=4,
        unit="percentunit",
        decimals=1,
        description="Appels LLM ou outils impossibles à attribuer à un agent : ils échappent au test A/B.",
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.01}],
    ),
    stat(
        "Spans orphelins",
        ORPHANS,
        7,
        58,
        w=3,
        unit="short",
        description=(
            "Spans dont le parent n'est jamais arrivé : export perdu, plantage, échantillonnage incohérent. "
            "Les 2 dernières minutes sont ignorées, le temps que les spans parents se terminent. Seul contrôle "
            "qui lit les spans bruts : avec l'échantillonnage, il porte sur les traces gardées."
        ),
        thresholds=ZERO_GOOD,
    ),
    stat(
        "Conversations fragmentées",
        FRAGMENTED,
        10,
        58,
        w=4,
        unit="short",
        description=(
            "Une même conversation répartie sur plusieurs traces : le contexte n'a pas suivi un sous-agent "
            "lancé dans un thread. Son coût n'est plus rattaché à l'exécution qui l'a délégué."
        ),
        thresholds=ZERO_GOOD,
    ),
    stat(
        "Attributs hérités renommés",
        """
SELECT sum(LegacyRenamed) AS spans
FROM otel.span_quality_1m
WHERE $__timeFilter(Minute)
""",
        14,
        58,
        w=4,
        unit="short",
        description="Spans dont le collector a converti des noms d'attributs anciens ou tiers vers les conventions actuelles.",
    ),
    stat(
        "Attributs supprimés",
        """
SELECT sum(RedactedAttributes) AS dropped
FROM otel.span_quality_1m
WHERE $__timeFilter(Minute)
""",
        18,
        58,
        w=3,
        unit="short",
        description="Attributs écartés par la liste blanche du collector. Une hausse signale une nouvelle instrumentation à examiner.",
    ),
    stat(
        "Durées négatives",
        """
SELECT sum(NegativeDurations) AS spans
FROM otel.span_quality_1m
WHERE $__timeFilter(Minute)
""",
        21,
        58,
        w=3,
        unit="short",
        description=(
            "Spans qui finissent avant d'avoir commencé : l'horloge de la machine qui les émet a reculé "
            "(synchronisation NTP, sortie de veille, Docker Desktop qui recale sa machine virtuelle). "
            "Leur durée compte pour 0 dans les vues ; sans ce garde-fou, une latence s'afficherait en siècles."
        ),
        thresholds=ZERO_GOOD,
    ),
    panel(
        "table",
        "Conversations fragmentées : traces à recoller",
        """
SELECT
    min(Timestamp) AS time,
    ConversationId AS conversation,
    uniqExact(TraceId) AS traces,
    groupUniqArray(AgentName) AS agents,
    argMinIf(TraceId, Timestamp, ParentAgent = '') AS main_trace,
    arrayFilter(t -> t != main_trace, groupUniqArray(TraceId))[1] AS detached_trace
FROM otel.genai_agent_runs
WHERE ConversationId != '' AND $__timeFilter(Timestamp)
GROUP BY ConversationId
HAVING traces > 1
ORDER BY time DESC
LIMIT 50
""",
        FORMAT_TABLE,
        (0, 62, 24, 7),
        options={"showHeader": True, "cellHeight": "sm"},
        overrides=[
            {
                "matcher": {"id": "byRegexp", "options": "main_trace|detached_trace"},
                "properties": [{"id": "links", "value": TRACE_LINK}],
            }
        ],
    ),
]

# --- Investigation ------------------------------------------------------------------
panels.append(row("Investigation", 69))
panels += [
    panel(
        "table",
        "Exécutions les plus chères (cliquez un identifiant de trace)",
        f"""
SELECT
    r.Timestamp AS time,
    r.AgentVersion AS version,
    r.TaskCategory AS category,
    r.Outcome AS outcome,
    r.Steps AS steps,
    r.InputTokens + r.OutputTokens AS tokens,
    c.CostUsd AS cost,
    r.DurationMs / 1000 AS duration,
    if(r.TraceId IN (SELECT TraceId FROM otel.otel_traces_trace_id_ts WHERE Start >= $__fromTime - INTERVAL 1 HOUR), 'oui', 'non') AS trace_kept,
    r.TraceId AS trace_id
FROM otel.genai_agent_runs AS r
INNER JOIN
(
    SELECT TraceId, sum(CostUsd) AS CostUsd
    FROM otel.genai_llm_calls_priced
    WHERE $__timeFilter(Timestamp)
    GROUP BY TraceId
) AS c ON r.TraceId = c.TraceId
WHERE r.{MAIN} AND $__timeFilter(r.Timestamp)
ORDER BY c.CostUsd DESC
LIMIT 20
""",
        FORMAT_TABLE,
        (0, 70, 24, 8),
        description=(
            "Les chiffres viennent des vues, calculées sur 100 % des spans. trace_kept indique si la trace brute "
            "est encore là : avec l'échantillonnage, seules les traces intéressantes et une part du reste sont gardées."
        ),
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
        "Trace (dernière exécution gardée, ou celle que vous avez cliquée)",
        """
WITH
    coalesce(
        nullIf(${trace_id:sqlstring}, ''),
        (
            SELECT argMax(TraceId, Timestamp)
            FROM otel.genai_agent_runs
            WHERE ParentAgent = '' AND $__timeFilter(Timestamp)
              AND TraceId IN (SELECT TraceId FROM otel.otel_traces_trace_id_ts WHERE Start >= $__fromTime - INTERVAL 1 HOUR)
        )
    ) AS trace_id,
    (SELECT min(Start) FROM otel.otel_traces_trace_id_ts WHERE TraceId = trace_id) AS trace_start,
    (SELECT max(End) + 1 FROM otel.otel_traces_trace_id_ts WHERE TraceId = trace_id) AS trace_end
SELECT
    TraceId AS traceID,
    SpanId AS spanID,
    ParentSpanId AS parentSpanID,
    ServiceName AS serviceName,
    SpanName AS operationName,
    multiply(toUnixTimestamp64Nano(Timestamp), 0.000001) AS startTime,
    multiply(greatest(toInt64(Duration), 0), 0.000001) AS duration,
    arrayMap(key -> map('key', key, 'value', SpanAttributes[key]), mapKeys(SpanAttributes)) AS tags,
    arrayMap(key -> map('key', key, 'value', ResourceAttributes[key]), mapKeys(ResourceAttributes)) AS serviceTags,
    if(StatusCode IN ('Error', 'STATUS_CODE_ERROR'), 2, 0) AS statusCode
FROM otel.otel_traces
WHERE TraceId = trace_id AND Timestamp >= trace_start AND Timestamp <= trace_end
ORDER BY startTime ASC
""",
        FORMAT_TRACE,
        (0, 78, 24, 16),
    ),
]

ALERTS_OUTPUT = Path(__file__).parent / "provisioning" / "alerting" / "sonde-alerts.yaml"

# Alert rules on the data-quality signals: a dashboard nobody watches catches nothing.
# Each query returns a time series; the rule reduces it over the window, then compares.
ALERT_RULES = [
    {
        "uid": "sonde-llm-sans-tokens",
        "title": "Appels LLM sans tokens",
        "sql": """
SELECT $__timeInterval(Timestamp) AS time, countIf(InputTokens = 0 AND StatusCode != 'Error') / count() AS ratio
FROM otel.genai_llm_calls
WHERE $__timeFilter(Timestamp)
GROUP BY time
ORDER BY time
""",
        "reducer": "mean",
        "threshold": 0.05,
        "summary": "Plus de 5 % des appels LLM réussis n'ont aucun token : coûts et tableaux de bord sont faux.",
        "description": "Un attribut gen_ai.usage.* a probablement changé de nom : ajoutez la correspondance dans "
        "transform/genai_normalize (otel-collector/config.yaml).",
    },
    {
        "uid": "sonde-spans-orphelins",
        "title": "Spans orphelins",
        "sql": f"""
SELECT $__timeInterval(Timestamp) AS time, count() AS orphans
FROM otel.otel_traces{ORPHAN_FILTER}
GROUP BY time
ORDER BY time
""",
        "reducer": "sum",
        "threshold": 0,
        "summary": "Des spans arrivent sans leur parent : des traces sont incomplètes.",
        "description": "Cherchez une perte à l'export (file pleine, collector redémarré) ou un échantillonnage "
        "décidé différemment selon les services.",
    },
    {
        "uid": "sonde-conversations-fragmentees",
        "title": "Conversations fragmentées",
        "sql": f"""
SELECT $__timeInterval(first_seen) AS time, count() AS conversations
FROM
({FRAGMENTED_CONVERSATIONS}
)
GROUP BY time
ORDER BY time
""",
        "reducer": "sum",
        "threshold": 0,
        "summary": "Des conversations sont réparties sur plusieurs traces : le contexte ne suit pas les sous-agents.",
        "description": "Un sous-agent lancé dans un thread ou un processus sans propagation du contexte "
        "(contextvars.copy_context, instrumentation threading, en-tête traceparent).",
    },
]


def build_alerts() -> dict:
    expr = {"type": "__expr__", "uid": "__expr__"}
    rules = []
    for rule in ALERT_RULES:
        rules.append(
            {
                "uid": rule["uid"],
                "title": rule["title"],
                "condition": "C",
                "data": [
                    {
                        "refId": "A",
                        "relativeTimeRange": {"from": 900, "to": 0},
                        "datasourceUid": DATASOURCE["uid"],
                        "model": {
                            **target(rule["sql"], FORMAT_TIMESERIES),
                            "intervalMs": 60000,
                            "maxDataPoints": 43200,
                        },
                    },
                    {
                        "refId": "B",
                        "datasourceUid": "__expr__",
                        "model": {
                            "refId": "B",
                            "type": "reduce",
                            "expression": "A",
                            "reducer": rule["reducer"],
                            "settings": {"mode": "dropNN"},
                            "datasource": expr,
                        },
                    },
                    {
                        "refId": "C",
                        "datasourceUid": "__expr__",
                        "model": {
                            "refId": "C",
                            "type": "threshold",
                            "expression": "B",
                            "conditions": [{"evaluator": {"type": "gt", "params": [rule["threshold"]]}}],
                            "datasource": expr,
                        },
                    },
                ],
                "dashboardUid": "sonde-agents",
                "noDataState": "OK",
                "execErrState": "Error",
                "for": "2m",
                "annotations": {"summary": rule["summary"], "description": rule["description"]},
                "labels": {"service": "sonde", "type": "qualite-donnees"},
            }
        )
    return {
        "apiVersion": 1,
        "groups": [{"orgId": 1, "name": "qualite-des-donnees", "folder": "Sonde", "interval": "1m", "rules": rules}],
    }


dashboard = {
    "uid": "sonde-agents",
    "title": "Sonde — observabilité des agents IA",
    "description": "Traces OpenTelemetry GenAI stockées dans ClickHouse : coût, qualité, fiabilité et sécurité d'un agent.",
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
                "label": "Identifiant de trace",
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
    # JSON is valid YAML: Grafana reads it from provisioning/alerting, no YAML library needed here.
    ALERTS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    ALERTS_OUTPUT.write_text(
        "# Generated by grafana/build_dashboard.py, do not edit by hand.\n"
        + json.dumps(build_alerts(), indent=2, ensure_ascii=False)
        + "\n"
    )
    print(f"wrote {ALERTS_OUTPUT} ({len(ALERT_RULES)} alert rules)")
