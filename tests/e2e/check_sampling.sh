#!/usr/bin/env bash
# Vérifie le profil d'échantillonnage en queue (docker-compose.sampling.yml) :
# les spans bruts sont échantillonnés, les chiffres restent exacts, rien d'utile n'est perdu.
# Lancer avec COMPOSE_FILE=docker-compose.yml:docker-compose.sampling.yml.
set -euo pipefail

summary=""
annotate() {
  local level=$1 body=$2
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::${level} title=check_sampling::${body//$'\n'/%0A}"
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
expect() {  # libellé, requête, condition sur la valeur (expression bash avec $v)
  local label=$1 sql=$2 condition=$3 v
  v=$(ch "$sql")
  printf '  %-44s %s\n' "$label" "$v"
  summary+="${label}: ${v}"$'\n'
  eval "[ $condition ]" || fail "$label = $v"
}

# Exécutions dont la décision d'échantillonnage est prise (decision_wait 30 s, plus les envois).
DECIDED="ParentAgent = '' AND Timestamp < now() - INTERVAL 60 SECOND"
KEPT="TraceId IN (SELECT TraceId FROM otel.otel_traces_trace_id_ts)"

echo "attente d'exécutions décidées et d'évaluations..."
for _ in $(seq 1 60); do
  decided=$(ch "SELECT count() FROM otel.genai_agent_runs WHERE ${DECIDED}" 2>/dev/null || echo 0)
  evals=$(ch "SELECT count() FROM otel.genai_evaluations" 2>/dev/null || echo 0)
  if [ "${decided:-0}" -ge 30 ] && [ "${evals:-0}" -ge 1 ]; then break; fi
  sleep 5
done

expect "exécutions décidées (vues, 100 %)" "SELECT count() FROM otel.genai_agent_runs WHERE ${DECIDED}" '$v -ge 30'
expect "dont trace brute gardée" "SELECT countIf(${KEPT}) FROM otel.genai_agent_runs WHERE ${DECIDED}" '$v -ge 1'
expect "dont trace brute écartée" "SELECT countIf(NOT ${KEPT}) FROM otel.genai_agent_runs WHERE ${DECIDED}" '$v -ge 1'
expect "échecs sans trace brute" "SELECT countIf(NOT ${KEPT}) FROM otel.genai_agent_runs WHERE ${DECIDED} AND Outcome != 'completed'" '$v -eq 0'
expect "évaluations (l'évaluateur lit les traces gardées)" "SELECT count() FROM otel.genai_evaluations" '$v -ge 1'
expect "appels LLM (vues)" "SELECT count() FROM otel.genai_llm_calls" '$v -ge 1'
expect "spans comptés (flux) > spans bruts" "SELECT (SELECT sum(Spans) FROM otel.span_quality_1m) > (SELECT count() FROM otel.otel_traces)" '$v -eq 1'

annotate notice "${summary}"
echo "OK"
