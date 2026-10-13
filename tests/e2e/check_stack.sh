#!/usr/bin/env bash
# Vérifie une stack démarrée de bout en bout :
# agent -> OTLP -> collector (normalisation, liste blanche, masquage) -> ClickHouse -> vues matérialisées,
# puis évaluateur -> gen_ai.evaluation.result -> genai_evaluations.
set -euo pipefail

summary=""
annotate() {  # GitHub annotation, readable from the run page and the API
  local level=$1 body=$2
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::${level} title=check_stack::${body//$'\n'/%0A}"
  return 0
}
fail() {
  annotate error "$1%0A${summary}"
  echo "ÉCHEC : $1" >&2
  exit 1
}

ch() {
  docker compose exec -T clickhouse sh -c \
    "clickhouse-client -u \"\$CLICKHOUSE_USER\" --password \"\$CLICKHOUSE_PASSWORD\" -q \"$1\""
}

check() {
  local label=$1 sql=$2 value
  value=$(ch "$sql")
  printf '  %-34s %s\n' "$label" "$value"
  summary+="${label}: ${value}"$'\n'
  [ "$(ch "SELECT ($sql) > 0")" = "1" ] || fail "$label = $value"
}

FRAGMENTED="SELECT count() FROM (SELECT ConversationId FROM otel.genai_agent_runs WHERE ConversationId != '' GROUP BY ConversationId HAVING uniqExact(TraceId) > 1)"

# La perte de contexte est aléatoire (SONDE_FAULT_LOST_CONTEXT_RATE) : on attend aussi qu'elle se produise.
echo "attente des exécutions de l'agent dans ClickHouse..."
for _ in $(seq 1 60); do
  runs=$(ch "SELECT count() FROM otel.genai_agent_runs WHERE ParentAgent = ''" 2>/dev/null || echo 0)
  subs=$(ch "SELECT count() FROM otel.genai_agent_runs WHERE ParentAgent != ''" 2>/dev/null || echo 0)
  fragmented=$(ch "$FRAGMENTED" 2>/dev/null || echo 0)
  if [ "${runs:-0}" -ge 10 ] && [ "${subs:-0}" -ge 1 ] && [ "${fragmented:-0}" -ge 1 ]; then break; fi
  sleep 5
done

# Un span au format d'une ancienne instrumentation : noms hérités et attribut inconnu.
trace_id=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
span_id=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
start=$(($(date +%s) * 1000000000))
curl -fsS -X POST http://localhost:4318/v1/traces -H 'Content-Type: application/json' -d @- >/dev/null <<JSON
{"resourceSpans": [{
  "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "legacy-app"}}]},
  "scopeSpans": [{"scope": {"name": "e2e"}, "spans": [{
    "traceId": "${trace_id}", "spanId": "${span_id}", "name": "chat legacy-model", "kind": 3,
    "startTimeUnixNano": "${start}", "endTimeUnixNano": "$((start + 250000000))",
    "attributes": [
      {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
      {"key": "gen_ai.system", "value": {"stringValue": "legacy-provider"}},
      {"key": "gen_ai.request.model", "value": {"stringValue": "legacy-model"}},
      {"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": "123"}},
      {"key": "gen_ai.usage.completion_tokens", "value": {"intValue": "45"}},
      {"key": "customer.email", "value": {"stringValue": "jane.doe@example.com"}}
    ]}]}]}]}
JSON
for _ in $(seq 1 20); do
  [ "$(ch "SELECT count() FROM otel.genai_llm_calls WHERE TraceId = '${trace_id}'")" = "1" ] && break
  sleep 2
done

check "spans bruts" "SELECT count() FROM otel.otel_traces"
check "exécutions principales" "SELECT count() FROM otel.genai_agent_runs WHERE ParentAgent = ''"
check "exécutions de sous-agents" "SELECT count() FROM otel.genai_agent_runs WHERE ParentAgent = 'sre-copilot'"
check "appels LLM" "SELECT count() FROM otel.genai_llm_calls"
check "appels d'outils" "SELECT count() FROM otel.genai_tool_calls"
check "agrégat à la minute (appels)" "SELECT sum(Calls) FROM otel.genai_usage_1m"
check "dépense LLM (USD)" "SELECT round(sum(CostUsd), 4) FROM otel.genai_cost_1m"
check "valeurs masquées" "SELECT sum(toUInt64OrZero(SpanAttributes['redaction.masked.count'])) FROM otel.otel_traces"
check "conversations fragmentées" "$FRAGMENTED"
check "compteurs de qualité (flux)" "SELECT sum(Spans) FROM otel.span_quality_1m"
check "span hérité normalisé" "SELECT count() FROM otel.genai_llm_calls WHERE TraceId = '${trace_id}' AND Provider = 'legacy-provider' AND InputTokens = 123 AND OutputTokens = 45"
check "marqueur sonde.semconv.legacy" "SELECT count() FROM otel.otel_traces WHERE TraceId = '${trace_id}' AND mapContains(SpanAttributes, 'sonde.semconv.legacy')"

# L'événement d'évaluation et le span de l'appel au juge arrivent par deux files distinctes.
echo "attente des premières évaluations du juge et de leur coût..."
for _ in $(seq 1 30); do
  evals=$(ch "SELECT count() FROM otel.genai_evaluations" 2>/dev/null || echo 0)
  judge=$(ch "SELECT count() FROM otel.genai_llm_calls WHERE AgentName = 'sonde-evaluator'" 2>/dev/null || echo 0)
  if [ "${evals:-0}" -ge 1 ] && [ "${judge:-0}" -ge 1 ]; then break; fi
  sleep 5
done
check "évaluations (gen_ai.evaluation.result)" "SELECT count() FROM otel.genai_evaluations"
check "évaluations rattachées à une exécution" "SELECT count() FROM otel.genai_evaluations AS e INNER JOIN otel.genai_agent_runs AS r ON e.TraceId = r.TraceId AND e.SpanId = r.SpanId"
check "coût du juge (USD)" "SELECT round(sum(CostUsd), 6) FROM otel.genai_cost_1m WHERE AgentName = 'sonde-evaluator'"

leaks=$(ch "SELECT count() FROM otel.otel_traces WHERE arrayExists(v -> v LIKE '%@example.%' OR v LIKE '%api_key=demo_%', mapValues(SpanAttributes))")
printf '  %-34s %s\n' "secrets en clair" "$leaks"
summary+="secrets en clair: ${leaks}"$'\n'
[ "$leaks" = "0" ] || fail "des secrets ont atteint ClickHouse"

dropped=$(ch "SELECT countIf(mapContains(SpanAttributes, 'customer.email') OR mapContains(SpanAttributes, 'gen_ai.system')) FROM otel.otel_traces WHERE TraceId = '${trace_id}'")
printf '  %-34s %s\n' "attributs hors liste conservés" "$dropped"
summary+="attributs hors liste conservés: ${dropped}"$'\n'
[ "$dropped" = "0" ] || fail "la liste blanche n'a pas filtré les attributs inconnus"

annotate notice "${summary}"
echo "OK"
