"""Resident SLA scanner that emits idempotent Outbox escalation events."""

from __future__ import annotations

import asyncio

from .metrics import RuntimeMetrics
from .tickets import TicketOperationsRepository
from .worker_metrics import WorkerMetricsDB


class SLAWorker:
    def __init__(
        self,
        repository: TicketOperationsRepository,
        *,
        interval_seconds: float = 30.0,
        batch_size: int = 100,
        metrics: RuntimeMetrics | None = None,
        worker_metrics: WorkerMetricsDB | None = None,
    ) -> None:
        if interval_seconds <= 0 or batch_size < 1 or batch_size > 1000:
            raise ValueError("SLA Worker 参数无效")
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.metrics = metrics or RuntimeMetrics(service_name="helpdesk-sla-worker")
        # 为 None 时跳过 DB 打点/心跳（测试或未启用可观测性）。
        self.worker_metrics = worker_metrics

    async def run_once(self) -> int:
        try:
            created = await self.repository.scan_sla_breaches(limit=self.batch_size)
        except Exception:
            self.metrics.increment("sla_scan_errors_total")
            if self.worker_metrics is not None:
                await self.worker_metrics.incr("sla_scan_errors_total")
            raise
        self.metrics.increment("sla_scan_runs_total")
        self.metrics.increment("sla_breach_events_total", created)
        if self.worker_metrics is not None:
            await self.worker_metrics.incr("sla_scan_runs_total")
            await self.worker_metrics.incr("ticket_sla_breach_total", amount=created)
        return created

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            await self.run_once()
            if self.worker_metrics is not None:
                await self.worker_metrics.beat("sla", "sla-worker")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
