"""Shared fixtures: an in-process fake LLM and an in-memory span exporter."""

from __future__ import annotations

import random
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "fake-llm"))

from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

import fake_llm  # noqa: E402
from sonde_agent import tools as tools_module  # noqa: E402
from sonde_agent.agent import Agent  # noqa: E402
from sonde_agent.llm import LLMClient  # noqa: E402
from sonde_agent.scenarios import build_variants  # noqa: E402
from sonde_agent.telemetry import setup_telemetry  # noqa: E402
from sonde_agent.tools import ToolRegistry, build_tools  # noqa: E402


@pytest.fixture(scope="session")
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    setup_telemetry(span_processor=SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture(scope="session")
def fake_llm_url():
    server = fake_llm.make_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture
def faults(monkeypatch):
    """Deterministic, instant fake LLM; tests override what they need."""
    original = fake_llm.Faults.snapshot()
    fake_llm.Faults.update(
        {
            "latency_factor": 0,
            "rate_limit_rate": 0,
            "error_rate": 0,
            "retry_after_s": 0,
            "loop_rate": 0,
            "hallucinated_tool_rate": 0,
            "parallel_tool_rate": 0,
            "small_model_dither_rate": 0,
        }
    )
    monkeypatch.setattr(tools_module.time, "sleep", lambda _s: None)
    yield fake_llm.Faults
    fake_llm.Faults.update(original)


@pytest.fixture
def make_agent(fake_llm_url, span_exporter, faults):
    span_exporter.clear()
    clients: list[LLMClient] = []

    def factory(version: str = "v1", capture: bool = True, seed: int = 7) -> Agent:
        rng = random.Random(seed)
        llm = LLMClient(fake_llm_url, provider="fake-llm", capture_content=capture, backoff_base_s=0)
        clients.append(llm)
        config = build_variants("sre-copilot", "sim-large", "sim-mini")[version]
        registry = ToolRegistry(build_tools(rng), capture_content=capture)
        return Agent(config, llm, registry, feedback_fn=lambda _outcome: 1)

    yield factory
    for client in clients:
        client.close()
