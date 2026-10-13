"""When prompts, tool arguments and tool results are recorded on spans.

    off      nothing is recorded (default in code)
    errors   content is recorded only where something went wrong: a failed
             LLM call, a failed or refused tool call, and the whole transcript
             of a run that did not complete (on its invoke_agent span)
    all      everything is recorded, on every span

`errors` keeps what is needed to debug a failure and nothing else. It is the
default of the docker-compose demo.
"""

from __future__ import annotations

import os
from enum import StrEnum


class Capture(StrEnum):
    OFF = "off"
    ERRORS = "errors"
    ALL = "all"

    @classmethod
    def coerce(cls, value: Capture | bool | str) -> Capture:
        if isinstance(value, bool):
            return cls.ALL if value else cls.OFF
        return cls(str(value).strip().lower())


def capture_from_env() -> Capture:
    """SONDE_CAPTURE_CONTENT wins; otherwise the standard OTel GenAI flag (true = all)."""
    value = os.getenv("SONDE_CAPTURE_CONTENT")
    if value:
        return Capture.coerce(value)
    legacy = os.getenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false").strip().lower()
    return Capture.ALL if legacy in {"1", "true", "yes", "on"} else Capture.OFF
