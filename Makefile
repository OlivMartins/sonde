.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help up infra down clean logs sql chaos calm dev test lint dashboard e2e package

help: ## Affiche cette aide
	@grep -E '^[a-z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up: ## Construit et démarre toute la stack, puis ouvrez http://localhost:3000
	$(COMPOSE) up -d --build

infra: ## Démarre le socle seul (ClickHouse, collector, Grafana), sans l'agent de démo
	$(COMPOSE) up -d clickhouse otel-collector grafana

down: ## Arrête la stack (garde les données)
	$(COMPOSE) down

clean: ## Arrête la stack et supprime toutes les données
	$(COMPOSE) down -v

logs: ## Suit le journal de l'agent : une ligne par exécution
	$(COMPOSE) logs -f agent

sql: ## Ouvre un shell ClickHouse sur la base otel
	$(COMPOSE) exec clickhouse sh -c 'clickhouse-client -u "$$CLICKHOUSE_USER" --password "$$CLICKHOUSE_PASSWORD" -d otel'

chaos: ## Provoque des limites de débit et des erreurs 5xx côté LLM
	@curl -s -X POST localhost:8080/admin/faults -d '{"rate_limit_rate": 0.4, "error_rate": 0.05}'; echo

calm: ## Rétablit le comportement normal du faux LLM
	@curl -s -X POST localhost:8080/admin/faults -d '{"rate_limit_rate": 0.04, "error_rate": 0.01}'; echo

dev: ## Installe les dépendances de développement dans l'environnement Python courant
	pip install -r requirements-dev.txt

test: ## Lance les tests unitaires et les tests SQL (chDB, sans serveur)
	python -m pytest -q tests

lint: ## Vérifie le style et le formatage
	ruff check . && ruff format --check .

dashboard: ## Régénère le JSON du tableau de bord depuis grafana/build_dashboard.py
	python grafana/build_dashboard.py

e2e: ## Vérifie une stack démarrée de bout en bout (flux de données et panneaux Grafana)
	bash tests/e2e/check_stack.sh
	python tests/e2e/check_grafana_panels.py

package: ## Construit l'archive de release dans dist/
	bash scripts/package.sh
