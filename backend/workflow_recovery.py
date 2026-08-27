"""Recover workflow intents after checkpoint/business commit split failures."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.my_agent.helpdesk import TicketCommand

from .metrics import RuntimeMetrics
from .tickets import TicketRepository


class WorkflowRecoveryWorker:
    def __init__(
        self,
        repository: TicketRepository,
        *,
        interval_seconds: float = 30.0,
        grace_seconds: int = 30,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        if interval_seconds <= 0 or grace_seconds < 1:
            raise ValueError("工作流恢复参数无效")
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.grace_seconds = grace_seconds
        self.metrics = metrics or RuntimeMetrics(service_name="helpdesk-workflow-recovery")

    async def run_once(self) -> tuple[int, int]:
        operations = await self.repository.list_recoverable_workflow_operations(
            older_than=datetime.now(timezone.utc) - timedelta(seconds=self.grace_seconds)
        )
        replayed = alerts = 0
        for operation in operations:
            intent = operation.get("intent")
            raw_commands = intent.get("commands") if isinstance(intent, dict) else None
            if not isinstance(raw_commands, list) or not raw_commands:
                alerts += 1
                continue
            commands = [TicketCommand.model_validate(item) for item in raw_commands]
            await self.repository.transition_many(
                operation["tenant_id"],
                commands,
                scopes={"ticket:system"},
                operation_id=operation["operation_id"],
            )
            replayed += 1
        self.metrics.increment("workflow_recovery_scanned_total", len(operations))
        self.metrics.increment("workflow_recovery_replayed_total", replayed)
        self.metrics.increment("workflow_recovery_manual_alert_total", alerts)
        return replayed, alerts

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
