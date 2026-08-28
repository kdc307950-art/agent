"""Recover workflow intents after checkpoint/business commit split failures."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from src.my_agent.helpdesk import TicketCommand

from .metrics import RuntimeMetrics
from .tickets import TicketRepository
from .worker_metrics import WorkerMetricsDB, safe_beat

logger = logging.getLogger(__name__)


class WorkflowRecoveryWorker:
    def __init__(
        self,
        repository: TicketRepository,
        *,
        interval_seconds: float = 30.0,
        grace_seconds: int = 30,
        metrics: RuntimeMetrics | None = None,
        worker_metrics: WorkerMetricsDB | None = None,
    ) -> None:
        if interval_seconds <= 0 or grace_seconds < 1:
            raise ValueError("工作流恢复参数无效")
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.grace_seconds = grace_seconds
        self.metrics = metrics or RuntimeMetrics(service_name="helpdesk-workflow-recovery")
        # 为 None 时跳过 DB 打点/心跳（测试或未启用可观测性）。
        self.worker_metrics = worker_metrics

    async def run_once(self) -> tuple[int, int, int]:
        operations = await self.repository.list_recoverable_workflow_operations(
            older_than=datetime.now(timezone.utc) - timedelta(seconds=self.grace_seconds)
        )
        replayed = alerts = failed = 0
        for operation in operations:
            intent = operation.get("intent")
            raw_commands = intent.get("commands") if isinstance(intent, dict) else None
            if not isinstance(raw_commands, list) or not raw_commands:
                alerts += 1
                await self.repository.mark_workflow_operation_failed(
                    tenant_id=operation["tenant_id"],
                    ticket_id=operation["ticket_id"],
                    operation_id=operation["operation_id"],
                    error_code="missing_intent",
                )
                continue
            try:
                commands = [TicketCommand.model_validate(item) for item in raw_commands]
                await self.repository.transition_many(
                    operation["tenant_id"],
                    commands,
                    scopes={"ticket:system"},
                    operation_id=operation["operation_id"],
                )
                replayed += 1
            except Exception as exc:
                failed += 1
                logger.exception("工作流恢复失败 tenant=%s ticket=%s operation=%s", operation["tenant_id"], operation["ticket_id"], operation["operation_id"])
                await self.repository.mark_workflow_operation_failed(
                    tenant_id=operation["tenant_id"],
                    ticket_id=operation["ticket_id"],
                    operation_id=operation["operation_id"],
                    error_code=type(exc).__name__,
                )
        self.metrics.increment("workflow_recovery_scanned_total", len(operations))
        self.metrics.increment("workflow_recovery_replayed_total", replayed)
        self.metrics.increment("workflow_recovery_manual_alert_total", alerts)
        self.metrics.increment("workflow_recovery_failed_total", failed)
        try:
            # 周期扫描顺手翻转过期 pending 追问（生产环境唯一执行点），失败不阻断恢复主流程。
            expired = await self.repository.expire_pending_intakes()
            if expired:
                await safe_incr(self.worker_metrics, "pending_intake_expired_total", amount=expired)
        except Exception:
            logger.exception("pending_expiry_failed")
        return replayed, alerts, failed

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
                await safe_incr(self.worker_metrics, "worker_loop_errors_total", {"worker": "recovery"})
                logger.exception(
                    "worker_round_failed",
                    extra={"ctx": {"worker_type": "recovery", "consecutive_failures": consecutive_failures}},
                )
                backoff = min(self.interval_seconds * (2 ** (consecutive_failures - 1)), 120.0)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                continue
            await safe_beat(self.worker_metrics, "recovery", "workflow-recovery")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
