#!/usr/bin/env bash
# Checks a running stack end to end:
# agent -> OTLP -> collector (redaction) -> ClickHouse -> materialized views.
set -euo pipefail

ch() {
  docker compose exec -T clickhouse sh -c \
    "clickhouse-client -u \"\$CLICKHOUSE_USER\" --password \"\$CLICKHOUSE_PASSWORD\" -q \"$1\""
}

echo "waiting for agent runs to land in ClickHouse..."
runs=0
for _ in $(seq 1 60); do
  runs=$(ch "SELECT count() FROM otel.genai_agent_runs" 2>/dev/null || echo 0)
  if [ "${runs:-0}" -ge 10 ]; then break; fi
  sleep 5
done

check() {
  local label=$1 sql=$2 value
  value=$(ch "$sql")
  printf '  %-28s %s\n' "$label" "$value"
  if [ "$(ch "SELECT ($sql) > 0")" != "1" ]; then
    echo "FAILED: $label" >&2
    exit 1
  fi
}

check "raw spans" "SELECT count() FROM otel.otel_traces"
check "agent runs" "SELECT count() FROM otel.genai_agent_runs"
check "llm calls" "SELECT count() FROM otel.genai_llm_calls"
check "tool calls" "SELECT count() FROM otel.genai_tool_calls"
check "usage rollup (calls)" "SELECT sum(Calls) FROM otel.genai_usage_1m"
check "llm spend (USD)" "SELECT round(sum(CostUsd), 4) FROM otel.genai_cost_1m"
check "masked secrets" "SELECT sum(toUInt64OrZero(SpanAttributes['redaction.masked.count'])) FROM otel.otel_traces"

leaks=$(ch "SELECT count() FROM otel.otel_traces WHERE arrayExists(v -> v LIKE '%@example.%' OR v LIKE '%api_key=demo_%', mapValues(SpanAttributes))")
printf '  %-28s %s\n' "unmasked secrets" "$leaks"
[ "$leaks" = "0" ] || { echo "FAILED: secrets reached ClickHouse" >&2; exit 1; }

echo "OK"
