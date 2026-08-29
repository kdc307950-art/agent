"""Resident SLA scanner that emits idempotent Outbox escalation events."""

from __future__ import annotations

import asyncio
import logging

from .metrics import RuntimeMetrics
from .tickets import TicketOperationsRepository
from .worker_metrics import WorkerMetricsDB, safe_beat, safe_incr

logger = logging.getLogger(__name__)


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
            await safe_incr(self.worker_metrics, "sla_scan_errors_total")
            raise
        self.metrics.increment("sla_scan_runs_total")
        self.metrics.increment("sla_breach_events_total", created)
        await safe_incr(self.worker_metrics, "sla_scan_runs_total")
        await safe_incr(self.worker_metrics, "ticket_sla_breach_total", amount=created)
        return created

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        consecutive_failures = 0
        while not stop_event.is_set():
            try:
                await self.run_once()
                consecutive_failures = 0
            except Exception:
                # 单轮失败（含 DB 扫描阶段不可用）不终止常驻进程：记录 + 计数 + 退避后继续下一轮。
                consecutive_failures += 1
                await safe_incr(self.worker_metrics, "worker_loop_errors_total", {"worker": "sla"})
                logger.exception(
                    "worker_round_failed",
                    extra={
                        "ctx": {"worker_type": "sla", "consecutive_failures": consecutive_failures}
                    },
                )
                backoff = min(self.interval_seconds * (2 ** (consecutive_failures - 1)), 120.0)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                continue
            await safe_beat(self.worker_metrics, "sla", "sla-worker")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
