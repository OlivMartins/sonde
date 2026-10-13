"""Runs every panel query of the dashboard through Grafana's query API.

Unlike tests/test_clickhouse_sql.py (chDB), this goes through the real
Grafana + ClickHouse plugin, so macros and result formats are exercised for real.
Standard library only.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GRAFANA = os.getenv("GRAFANA_URL", "http://localhost:3000")
AUTH = base64.b64encode(f"admin:{os.getenv('GRAFANA_ADMIN_PASSWORD', 'admin')}".encode()).decode()
DASHBOARD = Path(__file__).resolve().parents[2] / "grafana" / "dashboards" / "sonde-agents.json"


def annotate(level: str, message: str) -> None:
    """GitHub annotation, readable from the run page and the API."""
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::{level} title=grafana::{message.replace('%', '%25').replace(chr(10), '%0A')}")


def call(path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        GRAFANA + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def wait_for_grafana() -> None:
    for _ in range(60):
        try:
            if call("/api/health").get("database") == "ok":
                return
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(2)
    sys.exit("Grafana did not become healthy")


def main() -> int:
    wait_for_grafana()
    call("/api/dashboards/uid/sonde-agents")  # provisioned?

    failures = 0
    for panel in json.loads(DASHBOARD.read_text())["panels"]:
        for target in panel.get("targets", []):
            sql = target["rawSql"].replace("${trace_id:sqlstring}", "''")
            body = {"from": "now-1h", "to": "now", "queries": [{**target, "rawSql": sql, "intervalMs": 60000}]}
            try:
                result = call("/api/ds/query", body)["results"]["A"]
            except urllib.error.HTTPError as exc:
                result = {"error": f"HTTP {exc.code}: {exc.read().decode()[:300]}"}
            frames = result.get("frames", [])
            rows = sum(len(f.get("data", {}).get("values", [[]])[0] or []) for f in frames if f.get("data"))
            status = "ERROR " + result["error"] if "error" in result else f"{rows} rows"
            print(f"  {panel['title'][:60]:<60} {status}")
            if "error" in result:
                annotate("error", f"{panel['title']}: {result['error']}")
                failures += 1
    failures += check_alert_rules()
    print("OK" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


def check_alert_rules() -> int:
    """Provisioned alert rules exist and evaluate without error."""
    provisioned = {rule["title"] for rule in call("/api/v1/provisioning/alert-rules")}
    expected = {"Appels LLM sans tokens", "Spans orphelins", "Conversations fragmentées"}
    failures = 0
    if not expected <= provisioned:
        print(f"  alert rules missing: {sorted(expected - provisioned)}")
        failures += 1

    rules: list[dict] = []
    states: list[str] = []
    for _ in range(45):  # rules are evaluated every minute
        groups = call("/api/prometheus/grafana/api/v1/rules")["data"]["groups"]
        rules = [r for g in groups for r in g["rules"] if r["name"] in expected]
        if len(rules) == len(expected) and all(not r.get("lastEvaluation", "").startswith("0001") for r in rules):
            break
        time.sleep(4)
    for rule in rules:
        health, state = rule.get("health"), rule.get("state")
        print(f"  alert {rule['name']:<54} health={health} state={state}")
        states.append(f"{rule['name']}: health={health} state={state}")
        if health == "error":
            annotate("error", f"alerte {rule['name']}: {rule.get('lastError', '')[:500]}")
            failures += 1
    annotate("notice", "\n".join(states))
    return failures


if __name__ == "__main__":
    sys.exit(main())
