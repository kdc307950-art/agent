"""运行时指标 —— 进程内的指标采集器（线程安全）。

职责：
    - 记录 Agent 运行的计数、耗时、状态分布（Counter / Histogram 语义）
    - /metrics 端点通过 METRICS_AUTH_TOKEN 保护
    - 数据保留在内存（Counter + Lock），适合单机部署
"""

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
    """OpenTelemetry 指标门面，故意设计为低基数。

    指标标签只用 route/status/outcome/tool 这类通用概念，不分 tenant/user/run_id/prompt，
    防止无限膨胀的 cardinality 把 Prometheus 撑爆。基于这个原则，即使有
    1000 个租户、百万级请求也不会导致指标分析系统瘫痪。

    Prometheus 刮取通过 ``prometheus_payload()`` 暴露；OTLP 导出在配置
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` 时自动启用，可连到观测栈（Grafana/Jaeger）。
    本地测试用 ``snapshot()`` 取内存计数无需额外依赖。
    """

    def __init__(
        self,
        *,
        service_name: str | None = None,
        otlp_endpoint: str | None = None,
    ) -> None:
        self._counts: Counter[str] = Counter()  # 本地快照用（测试/诊断）
        self._lock = Lock()  # 多线程 FastAPI 下需要——创建计器/柱状图都要涉及 dict 改写
        self._counters = {}  # 名字→计数器对象，激活时创建（OTel 的对象难以销毁，故懒创）
        self._histograms = {}  # 名字→柱状图对象，激活时创建
        self._registry = CollectorRegistry()  # Prometheus 注册表，所有指标刮取从这里拿
        readers = [PrometheusMetricReader(registry=self._registry)]
        endpoint = (otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")).strip()
        self._otlp_reader = None
        # OTLP 是可选的补充导出（推送到观测后端）——生产跨集群才需要，
        # 本地开发和单机 Prometheus 都不需要。import 失败时 graceful degrade
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
        """记录计数器事件（如请求数、错误数）。"""
        if amount == 0:
            return
        attrs = self._attributes(attributes)
        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                counter = self._meter.create_counter(name, unit="{request}")
                self._counters[name] = counter
            self._counts[name] += amount  # 本地快照并发增长，同锁保护
        # 向 Prometheus/OTLP 推送——由于已注册到 registry，下次刮取会读到更新后的值
        counter.add(amount, attrs)

    def observe(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        """记录观测值（如延时、输入/输出 token 数）。"""
        attrs = self._attributes(attributes)
        with self._lock:
            histogram = self._histograms.get(name)
            if histogram is None:
                histogram = self._meter.create_histogram(name, unit="s")  # 时间单位默认秒
                self._histograms[name] = histogram
        # 懒创后即可无锁写入——柱状图对象本身是线程安全的
        histogram.record(value, attrs)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def prometheus_payload(self) -> tuple[bytes, str]:
        return generate_latest(self._registry), CONTENT_TYPE_LATEST

    def shutdown(self) -> None:
        self._provider.shutdown()
