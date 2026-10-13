"""Traffic the load generator replays: tasks, agent variants and user feedback."""

from __future__ import annotations

import random

from . import semconv as sc
from .agent import SYSTEM_PROMPT_V1, SYSTEM_PROMPT_V2, AgentConfig

TASKS: list[tuple[str, str]] = [
    ("latency", "checkout-service p95 latency doubled in the last 30 minutes, find out why."),
    ("latency", "payments-api is slow for EU customers since the last deploy."),
    ("latency", "search-api p99 is above 2s, is it the ClickHouse cluster?"),
    ("errors", "orders-api returns HTTP 500 for about 3% of requests, investigate."),
    ("errors", "payments-api throws connection pool exceptions after the PSP failover."),
    ("capacity", "Consumer lag on the kafka topic orders keeps growing."),
    ("capacity", "orders-api pods are close to their memory limit."),
    ("security", "We see a spike of 401 on auth-service, is it an attack?"),
    ("security", "Suspicious login attempts on auth-service from a few IPs."),
    ("vague", "Something feels off in production, can you take a look?"),
    ("vague", "Customers complain, check everything please."),
]

# Category mix: mostly well-scoped incidents, a few vague requests.
CATEGORY_WEIGHTS = {"latency": 0.3, "errors": 0.25, "capacity": 0.15, "security": 0.15, "vague": 0.15}


def build_variants(agent_name: str, v1_model: str, v2_model: str) -> dict[str, AgentConfig]:
    """v1 is the original agent; v2 is a cheaper candidate being A/B tested."""
    return {
        "v1": AgentConfig(name=agent_name, version="v1", model=v1_model, system_prompt=SYSTEM_PROMPT_V1, max_steps=8),
        "v2": AgentConfig(name=agent_name, version="v2", model=v2_model, system_prompt=SYSTEM_PROMPT_V2, max_steps=6),
    }


def pick_task(rng: random.Random) -> tuple[str, str]:
    categories = list(CATEGORY_WEIGHTS)
    category = rng.choices(categories, weights=[CATEGORY_WEIGHTS[c] for c in categories])[0]
    return category, rng.choice([text for cat, text in TASKS if cat == category])


def make_feedback_fn(rng: random.Random, version: str):
    """Simulated thumbs up (+1) / down (-1); ~60% of runs get rated."""
    satisfaction = {
        sc.OUTCOME_COMPLETED: 0.88 if version == "v1" else 0.82,
        sc.OUTCOME_MAX_STEPS: 0.2,
        sc.OUTCOME_LLM_ERROR: 0.05,
    }

    def feedback(outcome: str) -> int | None:
        if rng.random() > 0.6:
            return None
        return 1 if rng.random() < satisfaction.get(outcome, 0.5) else -1

    return feedback
