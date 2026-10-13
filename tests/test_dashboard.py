"""Static checks of the generated Grafana dashboard and provisioning."""

from __future__ import annotations

import json
from itertools import combinations

import yaml

from clickhouse_support import ROOT

DASHBOARD = json.loads((ROOT / "grafana" / "dashboards" / "sonde-agents.json").read_text())
DATASOURCE = yaml.safe_load((ROOT / "grafana" / "provisioning" / "datasources" / "clickhouse.yaml").read_text())


def test_panel_ids_are_unique():
    ids = [p["id"] for p in DASHBOARD["panels"]]
    assert len(ids) == len(set(ids))


def test_panels_do_not_overlap():
    def cells(panel):
        g = panel["gridPos"]
        return {(x, y) for x in range(g["x"], g["x"] + g["w"]) for y in range(g["y"], g["y"] + g["h"])}

    for a, b in combinations(DASHBOARD["panels"], 2):
        assert not cells(a) & cells(b), f"{a['title']} overlaps {b['title']}"
    assert all(p["gridPos"]["x"] + p["gridPos"]["w"] <= 24 for p in DASHBOARD["panels"])


def test_panels_use_the_provisioned_datasource():
    uid = DATASOURCE["datasources"][0]["uid"]
    for panel in DASHBOARD["panels"]:
        if panel["type"] == "row":
            continue
        assert panel["datasource"]["uid"] == uid
        assert all(t["datasource"]["uid"] == uid for t in panel["targets"])


def test_trace_links_point_to_this_dashboard():
    links = [
        prop["value"][0]["url"]
        for panel in DASHBOARD["panels"]
        for override in panel.get("fieldConfig", {}).get("overrides", [])
        for prop in override["properties"]
        if prop["id"] == "links"
    ]
    assert links
    assert all(url.startswith(f"/d/{DASHBOARD['uid']}?") and "var-trace_id=" in url for url in links)
