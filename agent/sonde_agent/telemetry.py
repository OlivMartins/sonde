"""OpenTelemetry setup.

Run-level attributes (agent name/version, conversation id, task category) are
carried in the OpenTelemetry Context and stamped on every span by a span
processor. They are deliberately *not* put in Baggage: Baggage is injected
into outgoing HTTP headers, which would leak them to the LLM provider.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import context, trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from . import __version__

_RUN_ATTRIBUTES_KEY = context.create_key("sonde.run_attributes")


class RunAttributesSpanProcessor(SpanProcessor):
    """Copies the current run's attributes onto every span started inside it."""

    def on_start(self, span, parent_context=None):
        attributes = context.get_value(_RUN_ATTRIBUTES_KEY, parent_context or context.get_current())
        if attributes:
            span.set_attributes(attributes)


def current_run_attributes() -> dict[str, str]:
    """Attributes of the run the caller is executing in ({} outside a run)."""
    return dict(context.get_value(_RUN_ATTRIBUTES_KEY) or {})


@contextmanager
def run_attributes(attributes: Mapping[str, str]) -> Iterator[None]:
    """Attach attributes that every span of the current run should carry."""
    token = context.attach(context.set_value(_RUN_ATTRIBUTES_KEY, dict(attributes)))
    try:
        yield
    finally:
        context.detach(token)


def setup_telemetry(
    exporter: SpanExporter | None = None, span_processor: SpanProcessor | None = None
) -> TracerProvider:
    """Configure the global tracer provider.

    By default spans go to the OTLP/HTTP exporter, configured by the standard
    OTEL_EXPORTER_OTLP_* environment variables. Tests pass their own processor.
    """
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "sre-copilot"),
            "service.version": __version__,
            "deployment.environment.name": os.getenv("DEPLOYMENT_ENVIRONMENT", "demo"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(RunAttributesSpanProcessor())
    if span_processor is None:
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter()
        span_processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(span_processor)
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return provider
