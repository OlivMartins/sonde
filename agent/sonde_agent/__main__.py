"""Load generator: runs the agent continuously against a few concurrent "users".

python -m sonde_agent                 # run forever (docker compose default)
python -m sonde_agent --runs 20       # run 20 tasks and exit
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import threading
import uuid

from .agent import Agent
from .llm import LLMClient
from .scenarios import build_variants, make_feedback_fn, pick_task
from .telemetry import setup_telemetry
from .tools import ToolRegistry, build_tools

log = logging.getLogger("sonde")


def env_flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sonde agent load generator")
    parser.add_argument("--runs", type=int, default=int(os.getenv("LOADGEN_RUNS", "0")), help="0 = run forever")
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("LOADGEN_CONCURRENCY", "3")))
    parser.add_argument("--min-pause", type=float, default=float(os.getenv("LOADGEN_MIN_PAUSE_S", "0.5")))
    parser.add_argument("--max-pause", type=float, default=float(os.getenv("LOADGEN_MAX_PAUSE_S", "3")))
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    provider = setup_telemetry()

    # Content capture is opt-in, as recommended by the GenAI conventions.
    capture = env_flag("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
    llm = LLMClient(
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:8080/v1"),
        api_key=os.getenv("LLM_API_KEY", ""),
        provider=os.getenv("LLM_PROVIDER", "fake-llm"),
        capture_content=capture,
    )
    variants = build_variants(
        agent_name=os.getenv("AGENT_NAME", "sre-copilot"),
        v1_model=os.getenv("AGENT_V1_MODEL", "sim-large"),
        v2_model=os.getenv("AGENT_V2_MODEL", "sim-mini"),
    )
    v2_share = float(os.getenv("AGENT_V2_TRAFFIC", "0.5"))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    budget = threading.Semaphore(args.runs) if args.runs > 0 else None

    def worker(index: int) -> None:
        rng = random.Random(None if args.seed is None else args.seed + index)
        tools = ToolRegistry(build_tools(rng), capture_content=capture)
        while not stop.is_set():
            if budget is not None and not budget.acquire(blocking=False):
                return
            version = "v2" if rng.random() < v2_share else "v1"
            config = variants[version]
            agent = Agent(config, llm, tools, feedback_fn=make_feedback_fn(rng, version))
            category, task = pick_task(rng)
            try:
                result = agent.run(task, conversation_id=str(uuid.uuid4()), category=category)
                log.info(
                    "%-19s %s/%-3s %-9s steps=%d tools=%d tokens=%d/%d trace=%s",
                    result.outcome,
                    config.name,
                    config.version,
                    category,
                    result.steps,
                    result.tool_calls,
                    result.usage.input_tokens,
                    result.usage.output_tokens,
                    result.trace_id,
                )
            except Exception:
                log.exception("agent run crashed")
            stop.wait(rng.uniform(args.min_pause, args.max_pause))

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(args.concurrency)]
    for thread in threads:
        thread.start()
    log.info(
        "load generator started: concurrency=%d runs=%s capture_content=%s",
        args.concurrency,
        args.runs or "inf",
        capture,
    )
    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(timeout=0.5)
    finally:
        stop.set()
        llm.close()
        provider.shutdown()


if __name__ == "__main__":
    main()
