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
            failures += "error" in result
    print("OK" if not failures else f"{failures} panel(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
