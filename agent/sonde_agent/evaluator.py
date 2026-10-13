"""Asynchronous LLM-as-a-judge.

Technical success is not answer quality. This service reads finished runs
that were sampled for evaluation (their question and answer are on the
invoke_agent span, already masked by the collector), asks a judge model to
score the answer, and emits the standard `gen_ai.evaluation.result` event.

The event is a log record parented to the evaluated span, so it lands in
ClickHouse (otel_logs) linked to its trace, and a materialized view turns it
into the genai_evaluations table. It runs outside the agent on purpose: a
judge call takes seconds and costs money, it must never slow the agent down.

    python -m sonde_agent.evaluator
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable

import httpx
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from . import semconv as sc
from .llm import LLMClient, LLMError
from .telemetry import run_attributes, setup_telemetry

log = logging.getLogger("sonde.evaluator")

EVALUATION_NAME = "relevance"
JUDGE_PROMPT = """You are an evaluator for an SRE copilot. Given the user's question and the
agent's answer, score from 1 to 5 how relevant and actionable the answer is:
5 = root cause, evidence and a concrete next step; 1 = vague or unusable.
Reply with JSON only: {"score": <1-5>, "label": "pass" | "fail", "explanation": "<one sentence>"}."""

# Sampled runs from the last 30 minutes, after the last one seen (keyset pagination on
# Timestamp, SpanId), with no evaluation yet (the NOT IN covers a restart of the evaluator).
CANDIDATES_SQL = """
SELECT
    toString(Timestamp) AS ts,
    TraceId,
    SpanId,
    SpanAttributes['gen_ai.input.messages'] AS question,
    SpanAttributes['gen_ai.output.messages'] AS answer
FROM otel.otel_traces
WHERE Timestamp >= now() - INTERVAL 30 MINUTE
  AND (Timestamp, SpanId) > (toDateTime64('{since}', 9), '{after}')
  AND SpanAttributes['gen_ai.operation.name'] = 'invoke_agent'
  AND SpanAttributes['sonde.eval.sampled'] = 'true'
  AND TraceId NOT IN (SELECT TraceId FROM otel.genai_evaluations WHERE Timestamp >= now() - INTERVAL 1 HOUR)
ORDER BY Timestamp, SpanId
LIMIT {limit}
"""
EPOCH = "1970-01-01 00:00:00"


def candidates_sql(limit: int, since: str = EPOCH, after: str = "") -> str:
    """`since` and `after` come from ClickHouse itself (a timestamp, a hex span id)."""
    return CANDIDATES_SQL.format(limit=int(limit), since=since.replace("'", ""), after=after.replace("'", ""))


def text_of(messages_json: str) -> str:
    """Concatenate the text parts of GenAI semconv messages."""
    try:
        messages = json.loads(messages_json or "[]")
    except json.JSONDecodeError:
        return messages_json
    return "\n".join(
        part.get("content", "")
        for message in messages
        for part in message.get("parts", [])
        if part.get("type") == "text"
    )


class ClickHouseHTTP:
    """Minimal ClickHouse client over the HTTP interface (no extra dependency)."""

    def __init__(self, url: str, user: str, password: str):
        self._http = httpx.Client(base_url=url, auth=(user, password), timeout=30)

    def query(self, sql: str) -> list[dict]:
        response = self._http.post("/", params={"default_format": "JSONEachRow"}, content=sql)
        response.raise_for_status()
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def close(self) -> None:
        self._http.close()


class Evaluator:
    def __init__(
        self,
        llm: LLMClient,
        judge_model: str,
        fetch: Callable[[str], list[dict]],
        logger_provider: LoggerProvider,
        batch_size: int = 20,
    ):
        self.llm = llm
        self.judge_model = judge_model
        self.fetch = fetch
        self.events = logger_provider.get_logger("sonde_agent.evaluator")
        self.batch_size = batch_size
        # Watermark: each poll starts after the last run seen. Evaluations take a few
        # seconds to reach ClickHouse, so the NOT IN of the query cannot be relied on
        # between two polls; and a run whose judgment failed is not retried forever.
        self._since, self._after = EPOCH, ""

    def candidates(self) -> list[dict]:
        return self.fetch(candidates_sql(self.batch_size, self._since, self._after))

    def _advance(self, rows: list[dict]) -> None:
        self._since, self._after = rows[-1]["ts"], rows[-1]["SpanId"]

    def judge(self, question: str, answer: str) -> dict:
        reply = self.llm.chat(
            model=self.judge_model,
            messages=[
                {"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": json.dumps({"question": question, "answer": answer})},
            ],
            tools=[],
            temperature=0.0,
            max_tokens=200,
            step=1,
        )
        verdict = json.loads(reply.message.get("content") or "{}")
        score = float(verdict["score"])
        return {
            "score": score,
            "label": str(verdict.get("label") or ("pass" if score >= 4 else "fail")),
            "explanation": str(verdict.get("explanation", "")),
        }

    def emit(self, trace_id: str, span_id: str, verdict: dict) -> None:
        evaluated = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        self.events.emit(
            timestamp=time.time_ns(),
            context=trace.set_span_in_context(NonRecordingSpan(evaluated)),
            event_name=sc.EVENT_EVALUATION_RESULT,
            severity_number=SeverityNumber.INFO,
            attributes={
                sc.GEN_AI_EVALUATION_NAME: EVALUATION_NAME,
                sc.GEN_AI_EVALUATION_SCORE_VALUE: verdict["score"],
                sc.GEN_AI_EVALUATION_SCORE_LABEL: verdict["label"],
                sc.GEN_AI_EVALUATION_EXPLANATION: verdict["explanation"],
                sc.SONDE_EVALUATION_JUDGE_MODEL: self.judge_model,
            },
        )

    def _judge(self, batch: list[dict]) -> int:
        emitted = 0
        # The judge's own LLM calls are traced like any agent's: evaluation has a cost too.
        with run_attributes({sc.GEN_AI_AGENT_NAME: "sonde-evaluator", sc.GEN_AI_AGENT_VERSION: "v1"}):
            for row in batch:
                try:
                    verdict = self.judge(text_of(row["question"]), text_of(row["answer"]))
                except (LLMError, KeyError, ValueError, json.JSONDecodeError) as exc:
                    log.warning("evaluation failed for trace %s: %s", row["TraceId"], exc)
                    continue
                self.emit(row["TraceId"], row["SpanId"], verdict)
                emitted += 1
        return emitted

    def run_once(self, rows: Iterable[dict] | None = None) -> int:
        """Judge one batch (the next candidates, or `rows`); returns the number of evaluations emitted.
        A failed judgment is logged and skipped: evaluation works on a sample anyway."""
        if rows is not None:
            return self._judge(list(rows))
        batch = self.candidates()
        emitted = self._judge(batch)
        if batch:
            self._advance(batch)
        return emitted

    def run_until_idle(self) -> int:
        """Judge every pending candidate, batch after batch."""
        total = 0
        while batch := self.candidates():
            total += self._judge(batch)
            self._advance(batch)
        return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    os.environ.setdefault("OTEL_SERVICE_NAME", "sonde-evaluator")
    tracer_provider = setup_telemetry()

    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    logger_provider = LoggerProvider(resource=tracer_provider.resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))

    clickhouse = ClickHouseHTTP(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_USER", "sonde"),
        os.getenv("CLICKHOUSE_PASSWORD", "sonde"),
    )
    llm = LLMClient(
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:8080/v1"),
        api_key=os.getenv("LLM_API_KEY", ""),
        provider=os.getenv("LLM_PROVIDER", "fake-llm"),
    )
    evaluator = Evaluator(llm, os.getenv("JUDGE_MODEL", "sim-judge"), clickhouse.query, logger_provider)
    interval = float(os.getenv("EVALUATOR_INTERVAL_S", "15"))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("evaluator started: judge=%s interval=%ss", evaluator.judge_model, interval)
    try:
        while not stop.is_set():
            try:
                emitted = evaluator.run_until_idle()
                if emitted:
                    log.info("emitted %d evaluation(s)", emitted)
            except httpx.HTTPError as exc:  # ClickHouse not ready yet, or restarting
                log.warning("cannot read candidates: %s", exc)
            stop.wait(interval)
    finally:
        llm.close()
        clickhouse.close()
        logger_provider.shutdown()
        tracer_provider.shutdown()


if __name__ == "__main__":
    main()
