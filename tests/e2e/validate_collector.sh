#!/usr/bin/env bash
# Valide chaque configuration du collector avec le binaire officiel :
# le socle, le gateway d'échantillonnage, et le socle avec chaque surcouche.
set -uo pipefail

IMAGE=otel/opentelemetry-collector-contrib:0.162.0
failures=0

validate() {
  local label=$1 out msg
  shift
  if out=$(docker run --rm \
      -e CLICKHOUSE_USER=x -e CLICKHOUSE_PASSWORD=x \
      -e LANGFUSE_OTLP_ENDPOINT=http://langfuse:3000/api/public/otel -e LANGFUSE_AUTH=eA== \
      -e PHOENIX_OTLP_ENDPOINT=http://phoenix:6006 \
      -v "$PWD/otel-collector:/cfg:ro" "$IMAGE" validate "$@" 2>&1); then
    echo "  ok      ${label}"
  else
    echo "  ÉCHEC   ${label}"
    echo "$out"
    msg=${out//'%'/'%25'}
    msg=${msg//$'\n'/'%0A'}
    [ -n "${GITHUB_ACTIONS:-}" ] && echo "::error title=validate ${label}::${msg: -3000}"
    failures=$((failures + 1))
  fi
}

validate "config.yaml" --config=/cfg/config.yaml
validate "gateway.yaml" --config=/cfg/gateway.yaml
for overlay in otel-collector/overlays/*.yaml; do
  name=${overlay##*/}
  validate "config.yaml + ${name}" --config=/cfg/config.yaml --config="/cfg/overlays/${name}"
done

exit $((failures > 0))
