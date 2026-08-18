from __future__ import annotations

import os
from collections import Counter
from threading import Lock
from typing import Mapping

from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

try:
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
except ImportError:  # pragma: no cover - optional OTLP dependency
    OTLPMetricExporter = None
    PeriodicExportingMetricReader = None


class RuntimeMetrics:
    """Low-cardinality OpenTelemetry metrics facade.

    ``snapshot`` remains for local tests and debugging. Prometheus scraping is
    exposed through ``prometheus_payload``; OTLP export is enabled when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is configured.
    """

    def __init__(
        self,
        *,
        service_name: str | None = None,
        otlp_endpoint: str | None = None,
    ) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()
        self._counters = {}
        self._histograms = {}
        self._registry = CollectorRegistry()
        readers = [PrometheusMetricReader(registry=self._registry)]
        endpoint = (otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")).strip()
        self._otlp_reader = None
        if endpoint and OTLPMetricExporter is not None and PeriodicExportingMetricReader is not None:
            exporter = OTLPMetricExporter(
                endpoint=endpoint,
                insecure=endpoint.startswith("http://"),
                timeout=5,
            )
            self._otlp_reader = PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=float(os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MILLIS", "15000")),
                export_timeout_millis=5000,
            )
            readers.append(self._otlp_reader)
        resource = Resource.create(
            {
                "service.name": service_name or os.getenv("OTEL_SERVICE_NAME", "langgraph-agent"),
                "service.version": os.getenv("APP_VERSION", "0.1.0"),
            }
        )
        self._provider = MeterProvider(resource=resource, metric_readers=readers)
        self._meter = self._provider.get_meter("langgraph.agent.runtime")

    @staticmethod
    def _attributes(attributes: Mapping[str, object] | None) -> dict[str, str | int | bool]:
        if not attributes:
            return {}
        return {
            str(key): value
            for key, value in attributes.items()
            if isinstance(value, (str, int, bool))
        }

    def increment(
        self,
        name: str,
        amount: int = 1,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        if amount == 0:
            return
        attrs = self._attributes(attributes)
        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                counter = self._meter.create_counter(name, unit="{request}")
                self._counters[name] = counter
            self._counts[name] += amount
        counter.add(amount, attrs)

    def observe(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        attrs = self._attributes(attributes)
        with self._lock:
            histogram = self._histograms.get(name)
            if histogram is None:
                histogram = self._meter.create_histogram(name, unit="s")
                self._histograms[name] = histogram
        histogram.record(value, attrs)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def prometheus_payload(self) -> tuple[bytes, str]:
        return generate_latest(self._registry), CONTENT_TYPE_LATEST

    def shutdown(self) -> None:
        self._provider.shutdown()
