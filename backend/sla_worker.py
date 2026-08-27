"""Resident SLA scanner that emits idempotent Outbox escalation events."""

from __future__ import annotations

import asyncio

from .metrics import RuntimeMetrics
from .tickets import TicketOperationsRepository


class SLAWorker:
    def __init__(
        self,
        repository: TicketOperationsRepository,
        *,
        interval_seconds: float = 30.0,
        batch_size: int = 100,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        if interval_seconds <= 0 or batch_size < 1 or batch_size > 1000:
            raise ValueError("SLA Worker 参数无效")
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.metrics = metrics or RuntimeMetrics(service_name="helpdesk-sla-worker")

    async def run_once(self) -> int:
        try:
            created = await self.repository.scan_sla_breaches(limit=self.batch_size)
        except Exception:
            self.metrics.increment("sla_scan_errors_total")
            raise
        self.metrics.increment("sla_scan_runs_total")
        self.metrics.increment("sla_breach_events_total", created)
        return created

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
