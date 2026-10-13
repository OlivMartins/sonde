"""Sub-agent delegation, run in a worker thread.

The SRE copilot can hand a log investigation to a `log-analyst` sub-agent.
The sub-agent runs in a thread pool, as it would in an agent framework that
fans work out. That is exactly where tracing usually breaks: Python copies
`contextvars` into asyncio tasks, but NOT into `ThreadPoolExecutor` workers.

    executor.submit(fn)                              # context lost: new, detached trace
    executor.submit(contextvars.copy_context().run, fn)  # context kept: nested spans

The correct form is used by default. `lost_context_rate` deliberately uses the
wrong one for a share of delegations, so the data-quality panels and alerts
have something real to detect: the same conversation split across traces,
and a sub-agent cost that no longer adds up to its parent run.
(opentelemetry-instrumentation-threading applies the same fix globally.)
"""

from __future__ import annotations

import contextvars
import functools
import random
from concurrent.futures import ThreadPoolExecutor

from . import semconv as sc
from .agent import Agent, AgentConfig
from .capture import Capture
from .llm import LLMClient
from .telemetry import current_run_attributes
from .tools import ToolRegistry, build_tools

LOG_ANALYST_PROMPT = """You are a log analyst working for an SRE copilot.
Search the logs of the service you are given, relate errors to known runbooks
and answer with the most likely root cause in two sentences."""

LOG_ANALYST_TOOLS = {"query_logs", "search_runbooks"}


class LogAnalystDelegate:
    """Tool handler for `ask_log_analyst`."""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        executor: ThreadPoolExecutor,
        rng: random.Random,
        model: str = "sim-mini",
        lost_context_rate: float = 0.0,
        timeout_s: float = 120.0,
    ):
        self.agent = Agent(
            AgentConfig(
                name="log-analyst",
                version="v1",
                model=model,
                system_prompt=LOG_ANALYST_PROMPT,
                max_steps=4,
                description="Sub-agent: searches logs and relates errors to runbooks.",
            ),
            llm,
            tools.subset(LOG_ANALYST_TOOLS),
        )
        self.executor = executor
        self.rng = rng
        self.lost_context_rate = lost_context_rate
        self.timeout_s = timeout_s

    def __call__(self, question: str, service: str = "", **_: object) -> dict:
        parent = current_run_attributes()
        work = functools.partial(
            self.agent.run,
            question,
            conversation_id=parent.get(sc.GEN_AI_CONVERSATION_ID, ""),
            category=parent.get(sc.SONDE_TASK_CATEGORY, "unknown"),
            parent_agent=parent.get(sc.GEN_AI_AGENT_NAME, ""),
        )
        if self.rng.random() < self.lost_context_rate:
            future = self.executor.submit(work)  # fault injection: the classic bug
        else:
            future = self.executor.submit(contextvars.copy_context().run, work)
        result = future.result(timeout=self.timeout_s)
        return {"agent": "log-analyst", "outcome": result.outcome, "answer": result.answer, "steps": result.steps}


def build_registry(
    llm: LLMClient,
    rng: random.Random,
    executor: ThreadPoolExecutor,
    capture: Capture | bool | str = False,
    sub_model: str = "sim-mini",
    lost_context_rate: float = 0.0,
) -> ToolRegistry:
    """The copilot's tools, including delegation to the log-analyst sub-agent."""
    base = ToolRegistry(build_tools(rng), capture)
    delegate = LogAnalystDelegate(llm, base, executor, rng, model=sub_model, lost_context_rate=lost_context_rate)
    return ToolRegistry(build_tools(rng, delegate=delegate), capture)
