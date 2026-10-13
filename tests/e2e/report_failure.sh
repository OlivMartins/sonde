#!/usr/bin/env bash
# En cas d'échec en CI, publie l'état des conteneurs et la fin de leurs journaux
# sous forme d'annotations GitHub, lisibles depuis l'onglet du run et par l'API.
set -uo pipefail

annotate() {
  local title=$1 body
  body=$(cat | tail -c 3500)
  body=${body//'%'/'%25'}
  body=${body//$'\r'/'%0D'}
  body=${body//$'\n'/'%0A'}
  echo "::error title=${title}::${body}"
}

[ -f up.log ] && annotate "docker compose up" < up.log
docker compose ps -a 2>&1 | annotate "docker compose ps"
for service in $(docker compose config --services); do
  docker compose logs --no-color --tail 50 "$service" 2>&1 | annotate "logs ${service}"
done
