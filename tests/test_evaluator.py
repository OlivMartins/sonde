"""The asynchronous judge: scores, the standard evaluation event, and its own cost."""

from __future__ import annotations

import json
import re

import pytest
from opentelemetry.trace import SpanKind

import fake_llm
from sonde_agent import semconv as sc
from sonde_agent.evaluator import EVALUATION_NAME, Evaluator, text_of
from sonde_agent.llm import LLMClient

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
QUESTION = "payments-api returns 5xx since the last deploy, what is going on?"
GOOD = fake_llm.FINAL_ANSWERS["errors"].format(svc="payments-api")
SHALLOW = fake_llm.FINAL_ANSWERS["shallow"].format(svc="payments-api")


def semconv_text(role: str, text: str) -> str:
    return json.dumps([{"role": role, "parts": [{"type": "text", "content": text}]}])


def candidate(answer: str, span_id: str = SPAN_ID) -> dict:
    return {
        "ts": "2026-10-13 09:00:00.000000000",
        "TraceId": TRACE_ID,
        "SpanId": span_id,
        "question": semconv_text("user", QUESTION),
        "answer": semconv_text("assistant", answer),
    }


@pytest.fixture
def evaluator(fake_llm_url, faults, span_exporter, log_exporter, logger_provider):
    span_exporter.clear()
    log_exporter.clear()
    llm = LLMClient(fake_llm_url, provider="fake-llm", backoff_base_s=0)
    yield Evaluator(llm, "sim-judge", fetch=lambda _sql: [], logger_provider=logger_provider)
    llm.close()


def test_text_of_reads_semconv_messages():
    assert text_of(semconv_text("user", "hello")) == "hello"
    assert text_of("not json") == "not json"
    assert text_of("") == ""


def test_judge_rewards_evidence_and_penalizes_vague_answers(evaluator):
    good = [evaluator.judge(QUESTION, GOOD)["score"] for _ in range(5)]
    shallow = [evaluator.judge(QUESTION, SHALLOW)["score"] for _ in range(5)]
    assert min(good) >= 3.8 and max(shallow) <= 2.4
    verdict = evaluator.judge(QUESTION, SHALLOW)
    assert verdict["label"] == "fail" and verdict["explanation"]


def test_evaluation_is_a_semconv_event_parented_to_the_judged_run(evaluator, log_exporter):
    assert evaluator.run_once([candidate(GOOD)]) == 1
    (record,) = log_exporter.get_finished_logs()
    log = record.log_record
    assert log.event_name == sc.EVENT_EVALUATION_RESULT == "gen_ai.evaluation.result"
    assert format(log.trace_id, "032x") == TRACE_ID and format(log.span_id, "016x") == SPAN_ID
    assert log.attributes[sc.GEN_AI_EVALUATION_NAME] == EVALUATION_NAME
    assert 1 <= log.attributes[sc.GEN_AI_EVALUATION_SCORE_VALUE] <= 5
    assert log.attributes[sc.GEN_AI_EVALUATION_SCORE_LABEL] == "pass"
    assert log.attributes[sc.SONDE_EVALUATION_JUDGE_MODEL] == "sim-judge"
    assert log.timestamp


def test_judge_calls_are_traced_and_attributed_like_any_agent(evaluator, span_exporter):
    evaluator.run_once([candidate(GOOD)])
    (chat,) = [
        s for s in span_exporter.get_finished_spans() if s.attributes.get(sc.GEN_AI_OPERATION_NAME) == sc.OP_CHAT
    ]
    assert chat.name == "chat sim-judge" and chat.kind == SpanKind.CLIENT
    assert chat.attributes[sc.GEN_AI_AGENT_NAME] == "sonde-evaluator"
    assert chat.attributes[sc.GEN_AI_USAGE_INPUT_TOKENS] > 0
    assert chat.parent is None  # its own trace: the judged run is not made longer or costlier


def test_polling_moves_forward_and_never_judges_twice(fake_llm_url, faults, logger_provider, log_exporter):
    # ClickHouse keeps returning the same rows until the evaluations land: the watermark skips them.
    broken = {"ts": "2026-10-13 09:00:00.000000000", "TraceId": TRACE_ID, "SpanId": "c" * 16}  # no answer
    later = {**candidate(SHALLOW, span_id="d" * 16), "ts": "2026-10-13 09:00:01.000000000"}
    table = [candidate(GOOD), broken, later]
    queries: list[str] = []

    def fetch(sql: str) -> list[dict]:  # the keyset condition of CANDIDATES_SQL, then LIMIT 2
        queries.append(sql)
        watermark = re.search(r"> \(toDateTime64\('([^']*)', 9\), '([^']*)'\)", sql).groups()
        rows = [r for r in table if (r["ts"], r["SpanId"]) > watermark]
        return sorted(rows, key=lambda r: (r["ts"], r["SpanId"]))[:2]

    llm = LLMClient(fake_llm_url, provider="fake-llm", backoff_base_s=0)
    evaluator = Evaluator(llm, "sim-judge", fetch=fetch, logger_provider=logger_provider, batch_size=2)
    log_exporter.clear()
    assert evaluator.run_until_idle() == 2  # the broken row is skipped, not retried forever
    assert evaluator.run_until_idle() == 0
    judged = [format(r.log_record.span_id, "016x") for r in log_exporter.get_finished_logs()]
    assert judged == [SPAN_ID, "d" * 16]
    assert "(toDateTime64('2026-10-13 09:00:01.000000000', 9), 'dddddddddddddddd')" in queries[-1]
    llm.close()
