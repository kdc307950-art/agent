"""OpenTelemetry 链路追踪 —— API 及出站模型/工具 HTTP 调用。"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class Telemetry:
    """Own the process-wide tracer provider and instrumentation lifecycle."""

    def __init__(self, app) -> None:
        self.app = app
        self._instrumented = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()) or os.getenv(
            "OTEL_TRACES_ENABLED", "false"
        ).lower() in {"1", "true", "yes"}
        service_name = os.getenv("OTEL_SERVICE_NAME", "langgraph-agent")
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": os.getenv("APP_VERSION", "0.1.0"),
                "deployment.environment": os.getenv("APP_ENV", "development"),
            }
        )
        self.provider = TracerProvider(resource=resource)
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if endpoint:
            exporter = OTLPSpanExporter(
                endpoint=endpoint,
                insecure=endpoint.startswith("http://"),
                timeout=5,
            )
            self.provider.add_span_processor(BatchSpanProcessor(exporter))
        if self._instrumented:
            try:
                trace.set_tracer_provider(self.provider)
            except Exception:
                # A process-wide provider may already be installed by the host.
                # FastAPI/HTTPX instrumentation still uses this provider below.
                pass
        self._httpx_instrumentor = HTTPXClientInstrumentor()
        if self._instrumented:
            FastAPIInstrumentor.instrument_app(app, tracer_provider=self.provider)
            self._httpx_instrumentor.instrument(tracer_provider=self.provider)

    def shutdown(self) -> None:
        if self._instrumented:
            FastAPIInstrumentor.uninstrument_app(self.app)
            self._httpx_instrumentor.uninstrument()
        self.provider.shutdown()
