"""The collector keeps every attribute the agent emits, and nothing else.

The redaction processor runs fail-closed (allow_all_keys: false): a key that is
not listed is dropped. These tests make sure the list follows the code, so a new
attribute cannot silently disappear between the agent and ClickHouse.
"""

from __future__ import annotations

import re

import pytest
import yaml

from clickhouse_support import COLLECTOR_CONFIG, Redaction

COLLECTOR = COLLECTOR_CONFIG.parent
CONFIG = yaml.safe_load(COLLECTOR_CONFIG.read_text())
ALLOWED = set(CONFIG["processors"]["redaction/genai"]["allowed_keys"])
PIPELINES = CONFIG["service"]["pipelines"]
CONTENT_KEYS = {
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.definitions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
}


def merged(base: dict, overlay: dict) -> dict:
    """How the collector merges several --config files: maps are merged, lists replaced."""
    out = dict(base)
    for key, value in overlay.items():
        out[key] = merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def overlay(name: str) -> dict:
    return merged(CONFIG, yaml.safe_load((COLLECTOR / "overlays" / name).read_text()))


def emitted_keys(spans) -> set[str]:
    keys: set[str] = set()
    for span in spans:
        keys |= set(span.attributes) | set(span.resource.attributes)
        for event in span.events:
            keys |= set(event.attributes)
    return keys


def test_every_attribute_the_agent_emits_is_allowed(replayed_spans):
    missing = emitted_keys(replayed_spans) - ALLOWED
    assert not missing, f"add to allowed_keys in otel-collector/config.yaml: {sorted(missing)}"


def test_unknown_attributes_are_dropped_and_counted():
    out = Redaction()({"gen_ai.request.model": "m", "customer.email": "jane@example.com", "gen_ai.system": "x"})
    assert out["gen_ai.request.model"] == "m"
    assert "customer.email" not in out and "gen_ai.system" not in out
    assert out["redaction.redacted.count"] == "2"


def test_secret_looking_values_are_masked_in_allowed_keys():
    out = Redaction()({"gen_ai.tool.call.result": "user=jane@example.com api_key=demo_123 Bearer abc.def"})
    assert out["gen_ai.tool.call.result"] == "user=**** **** ****"  # the key name goes too
    assert out["redaction.masked.count"] == "1"


def test_normalization_runs_first_and_its_targets_survive_the_allow_list():
    processors = PIPELINES["traces/in"]["processors"]
    assert processors.index("transform/genai_normalize") < processors.index("redaction/genai")

    statements = CONFIG["processors"]["transform/genai_normalize"]["trace_statements"]
    targets = {re.match(r'set\(span\.attributes\["([^"]+)"\]', s)[1] for s in statements}
    sources = {m for s in statements for m in re.findall(r'span\.attributes\["([^"]+)"\] != nil', s)}
    assert targets <= ALLOWED
    # legacy names are renamed, then dropped: only the canonical name is stored
    assert not (sources - targets) & ALLOWED


def test_every_branch_receives_spans_after_redaction():
    """Normalization and redaction run once, before the fan-out: no branch sees unfiltered data."""
    assert PIPELINES["traces/in"]["receivers"] == ["otlp"]
    assert PIPELINES["traces/in"]["processors"][-1] == "redaction/genai"
    assert PIPELINES["traces/in"]["exporters"] == ["forward"]
    assert PIPELINES["logs"]["processors"][-1] == "redaction/genai"
    for name in ("traces/aggregates", "traces/raw"):
        assert PIPELINES[name]["receivers"] == ["forward"]


def test_aggregate_branch_keeps_numbers_and_drops_content():
    assert PIPELINES["traces/aggregates"]["exporters"] == ["clickhouse/stream"]
    assert CONFIG["exporters"]["clickhouse/stream"]["traces_table_name"] == "otel_spans_stream"
    statements = CONFIG["processors"]["transform/drop_content"]["trace_statements"]
    deleted = {re.fullmatch(r'delete_key\(span\.attributes, "([^"]+)"\)', s)[1] for s in statements}
    assert deleted == CONTENT_KEYS
    assert CONTENT_KEYS <= ALLOWED  # kept in the raw branch, for drill-down and the evaluator


def test_evaluation_events_keep_their_attributes(logger_provider, log_exporter):
    from sonde_agent.evaluator import Evaluator

    log_exporter.clear()
    evaluator = Evaluator(llm=None, judge_model="sim-judge", fetch=lambda _sql: [], logger_provider=logger_provider)
    evaluator.emit(
        "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331", {"score": 4.0, "label": "pass", "explanation": "ok"}
    )
    (record,) = log_exporter.get_finished_logs()
    assert set(record.log_record.attributes) <= ALLOWED


def test_sampling_overlay_only_reroutes_the_raw_branch():
    config = overlay("sampling-edge.yaml")
    pipelines = config["service"]["pipelines"]
    assert pipelines["traces/raw"] == {"receivers": ["forward"], "exporters": ["loadbalancing"]}
    assert pipelines["traces/aggregates"] == PIPELINES["traces/aggregates"]  # costs stay exact
    assert pipelines["traces/in"] == PIPELINES["traces/in"]
    # all spans of a trace must reach the same gateway, or tail sampling sees partial traces
    assert config["exporters"]["loadbalancing"]["routing_key"] == "traceID"


def test_gateway_keeps_every_trace_the_evaluator_and_investigations_need():
    gateway = yaml.safe_load((COLLECTOR / "gateway.yaml").read_text())
    assert gateway["service"]["pipelines"]["traces"]["processors"][-1] == "tail_sampling"
    assert gateway["exporters"]["clickhouse"]["traces_table_name"] == "otel_traces"
    policies = {p["name"]: p for p in gateway["processors"]["tail_sampling"]["policies"]}
    assert policies["errors"]["status_code"]["status_codes"] == ["ERROR"]
    assert policies["refused-tools"]["boolean_attribute"]["key"] == "sonde.tool.blocked"
    assert policies["sampled-for-evaluation"]["boolean_attribute"]["key"] == "sonde.eval.sampled"
    # tests/clickhouse_support.kept_by_tail_sampling mirrors these thresholds
    assert policies["retried"]["numeric_attribute"]["min_value"] == 1
    assert policies["expensive"]["numeric_attribute"]["min_value"] == 12000
    assert policies["baseline"]["type"] == "probabilistic"
    # the sampling attributes survive the edge collector's allow-list
    used = {p[t]["key"] for p in policies.values() for t in ("boolean_attribute", "numeric_attribute") if t in p}
    assert used <= ALLOWED


@pytest.mark.parametrize("name", ["langfuse.yaml", "phoenix.yaml"])
def test_interop_overlays_export_a_redacted_copy(name):
    config = overlay(name)
    (extra,) = set(config["service"]["pipelines"]) - set(PIPELINES)
    pipeline = config["service"]["pipelines"][extra]
    assert pipeline["receivers"] == ["forward"]  # after normalization and redaction
    (exporter,) = pipeline["exporters"]
    assert exporter.startswith("otlphttp/") and exporter in config["exporters"]
    assert config["service"]["pipelines"]["traces/raw"] == PIPELINES["traces/raw"]
