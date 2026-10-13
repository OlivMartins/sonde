"""The collector keeps every attribute the agent emits, and nothing else.

The redaction processor runs fail-closed (allow_all_keys: false): a key that is
not listed is dropped. These tests make sure the list follows the code, so a new
attribute cannot silently disappear between the agent and ClickHouse.
"""

from __future__ import annotations

import re

import yaml

from clickhouse_support import COLLECTOR_CONFIG, Redaction

CONFIG = yaml.safe_load(COLLECTOR_CONFIG.read_text())
ALLOWED = set(CONFIG["processors"]["redaction/genai"]["allowed_keys"])


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
    processors = CONFIG["service"]["pipelines"]["traces"]["processors"]
    assert processors.index("transform/genai_normalize") < processors.index("redaction/genai")

    statements = CONFIG["processors"]["transform/genai_normalize"]["trace_statements"]
    targets = {re.match(r'set\(span\.attributes\["([^"]+)"\]', s)[1] for s in statements}
    sources = {m for s in statements for m in re.findall(r'span\.attributes\["([^"]+)"\] != nil', s)}
    assert targets <= ALLOWED
    # legacy names are renamed, then dropped: only the canonical name is stored
    assert not (sources - targets) & ALLOWED
