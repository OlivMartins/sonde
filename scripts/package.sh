#!/usr/bin/env bash
# Construit les fichiers joints à une release, de façon reproductible :
#   dist/sonde-<version>.tar.gz          la stack complète, prête à `docker compose up`
#   dist/sonde-<version>.tar.gz.sha256   sa somme de contrôle
#   dist/sonde-dashboard.json            le tableau de bord, importable dans un Grafana existant
set -euo pipefail

cd "$(dirname "$0")/.."
version=$(sed -n 's/^version = "\(.*\)"/\1/p' agent/pyproject.toml)
name="sonde-${version}"

rm -rf dist && mkdir -p dist
git archive --format=tar --prefix="${name}/" HEAD | gzip -n -9 > "dist/${name}.tar.gz"
(cd dist && sha256sum "${name}.tar.gz" > "${name}.tar.gz.sha256")
cp grafana/dashboards/sonde-agents.json dist/sonde-dashboard.json

ls -l dist
