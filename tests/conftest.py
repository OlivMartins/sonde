"""Shared fixtures: an in-process fake LLM and an in-memory span exporter."""

from __future__ import annotations

import random
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "fake-llm"))

from opentelemetry.sdk._logs import LoggerProvider  # noqa: E402
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

import fake_llm  # noqa: E402
from sonde_agent import tools as tools_module  # noqa: E402
from sonde_agent.agent import Agent  # noqa: E402
from sonde_agent.delegation import build_registry  # noqa: E402
from sonde_agent.llm import LLMClient  # noqa: E402
from sonde_agent.scenarios import build_variants, make_feedback_fn, pick_task  # noqa: E402
from sonde_agent.telemetry import setup_telemetry  # noqa: E402
from sonde_agent.tools import ToolRegistry, build_tools  # noqa: E402


@pytest.fixture(scope="session")
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    setup_telemetry(span_processor=SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture(scope="session")
def log_exporter() -> InMemoryLogRecordExporter:
    return InMemoryLogRecordExporter()


@pytest.fixture(scope="session")
def logger_provider(log_exporter) -> LoggerProvider:
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    return provider


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
            "small_model_shallow_rate": 0,
        }
    )
    monkeypatch.setattr(tools_module.time, "sleep", lambda _s: None)
    yield fake_llm.Faults
    fake_llm.Faults.update(original)


@pytest.fixture(scope="session")
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sub-agent")
    yield pool
    pool.shutdown(wait=True)


@pytest.fixture
def make_agent(fake_llm_url, span_exporter, faults, executor):
    span_exporter.clear()
    clients: list[LLMClient] = []

    def factory(
        version: str = "v1",
        capture: bool | str = True,
        seed: int = 7,
        delegation: bool = False,
        lost_context_rate: float = 0.0,
        eval_sample_rate: float = 0.0,
    ) -> Agent:
        rng = random.Random(seed)
        llm = LLMClient(fake_llm_url, provider="fake-llm", capture_content=capture, backoff_base_s=0)
        clients.append(llm)
        config = build_variants("sre-copilot", "sim-large", "sim-mini")[version]
        if delegation:
            registry = build_registry(llm, rng, executor, capture=capture, lost_context_rate=lost_context_rate)
        else:
            registry = ToolRegistry(build_tools(rng), capture_content=capture)
        return Agent(
            config, llm, registry, feedback_fn=lambda _outcome: 1, eval_sample_rate=eval_sample_rate, eval_rng=rng
        )

    yield factory
    for client in clients:
        client.close()


REPLAY_RUNS = 60


@pytest.fixture(scope="session")
def replayed_spans(span_exporter, fake_llm_url, executor):
    """A realistic mix of agent runs, shared by the ClickHouse and collector tests:
    both agent versions, every task category, rate limits, loops, refused tools,
    sub-agent delegation (30% of it with the context lost), content captured,
    half of the completed runs sampled for the evaluator."""
    original = fake_llm.Faults.snapshot()
    fake_llm.Faults.update(
        {
            "latency_factor": 0,
            "retry_after_s": 0,
            "rate_limit_rate": 0.1,
            "error_rate": 0.02,
            "loop_rate": 0.75,
            "hallucinated_tool_rate": 1,
            "parallel_tool_rate": 0.3,
            "small_model_dither_rate": 0.25,
            "small_model_shallow_rate": 0.3,
        }
    )
    fake_llm.Handler.rng.seed(42)
    span_exporter.clear()
    rng = random.Random(42)
    eval_rng = random.Random(43)
    variants = build_variants("sre-copilot", "sim-large", "sim-mini")
    llm = LLMClient(fake_llm_url, provider="fake-llm", capture_content="all", max_retries=2, backoff_base_s=0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tools_module.time, "sleep", lambda _s: None)
        registry = build_registry(llm, rng, executor, capture="all", lost_context_rate=0.3)
        for _ in range(REPLAY_RUNS):
            version = rng.choice(["v1", "v2"])
            category, task = pick_task(rng)
            agent = Agent(
                variants[version],
                llm,
                registry,
                feedback_fn=make_feedback_fn(rng, version),
                eval_sample_rate=0.5,
                eval_rng=eval_rng,
            )
            agent.run(task, conversation_id=str(uuid.uuid4()), category=category)
    llm.close()
    fake_llm.Faults.update(original)
    finished = list(span_exporter.get_finished_spans())
    span_exporter.clear()
    return finished
