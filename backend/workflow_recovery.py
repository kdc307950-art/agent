"""工作流意图恢复 Worker —— 修复检查点提交与业务提交不一致导致的中断。

职责：
    - 扫描「已写 checkpointer 但业务侧提交失败」的 workflow 操作记录
    - 重放其中保存的工单命令（TicketCommand 列表），使业务状态收敛
    - 顺带翻转过期的 pending 追问（intake），保持待办队列干净

关键设计：
    - 宽限期：操作记录需停留在「未完成」超过 grace_seconds 才被扫描，
      给正常提交留出缓冲，避免与在线执行竞争
    - 命令重放幂等：transition_many 携带 operation_id 作为幂等键，
      恢复重放与在线执行共用同一套去重逻辑，重复执行不会重复生效
    - 异常隔离：单条操作重放失败只标记该操作为失败（mark_workflow_operation_failed），
      不阻断同批其它操作；恢复主流程的失败也不影响 pending 追问翻转
    - 常驻循环：单轮失败记录 + 指数退避（封顶 120 秒）后继续
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from src.my_agent.helpdesk import TicketCommand

from .metrics import RuntimeMetrics
from .tickets import TicketRepository
from .worker_metrics import WorkerMetricsDB, safe_beat, safe_incr

logger = logging.getLogger(__name__)


class WorkflowRecoveryWorker:
    """工作流意图恢复 Worker。

    构造参数：repository 提供 list_recoverable_workflow_operations 等操作；
    interval_seconds 扫描间隔（默认 30 秒）；grace_seconds 宽限期（默认 30 秒，
    操作记录停留超此时长才视为可恢复）；metrics/worker_metrics 为可选指标通道。
    """

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
        """执行一轮恢复扫描，返回 (replayed, alerts, failed) 统计。

        返回：三元组 —— replayed 成功重放数；alerts 意图缺失/命令非法需人工
        介入数；failed 重放失败数。
        设计：仅扫描超过宽限期仍未完成的操作；intent 缺失或 commands 为空
        直接标记 failed（error_code=missing_intent）并计入 alerts。
        """
        operations = await self.repository.list_recoverable_workflow_operations(
            older_than=datetime.now(UTC) - timedelta(seconds=self.grace_seconds)
        )
        replayed = alerts = failed = 0
        for operation in operations:
            intent = operation.get("intent")
            raw_commands = intent.get("commands") if isinstance(intent, dict) else None
            if not isinstance(raw_commands, list) or not raw_commands:
                # 意图数据缺失或没有命令可重放：无法自动恢复，转人工告警。
                alerts += 1
                await self.repository.mark_workflow_operation_failed(
                    tenant_id=operation["tenant_id"],
                    ticket_id=operation["ticket_id"],
                    operation_id=operation["operation_id"],
                    error_code="missing_intent",
                )
                continue
            try:
                # 反序列化命令列表后以 operation_id 幂等重放，收敛业务状态。
                commands = [TicketCommand.model_validate(item) for item in raw_commands]
                await self.repository.transition_many(
                    operation["tenant_id"],
                    commands,
                    scopes={"ticket:system"},
                    operation_id=operation["operation_id"],
                )
                replayed += 1
            except Exception as exc:
                # 单条重放失败：记录日志并标记失败，继续处理同批其它操作。
                failed += 1
                logger.exception(
                    "工作流恢复失败 tenant=%s ticket=%s operation=%s",
                    operation["tenant_id"],
                    operation["ticket_id"],
                    operation["operation_id"],
                )
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
        """常驻循环：按 interval_seconds 周期执行恢复扫描，直到收到停止信号。

        参数：stop_event 停止信号（SIGINT/SIGTERM 时由入口设置）。
        设计：单轮异常不退出进程——计数 + 指数退避（封顶 120 秒）后继续；
        每轮成功结束后上报心跳指标。
        """
        stop_event = stop_event or asyncio.Event()
        consecutive_failures = 0
        while not stop_event.is_set():
            try:
                await self.run_once()
                consecutive_failures = 0
            except Exception:
                # 单轮失败（含 DB 扫描阶段不可用）不终止常驻进程：记录 + 计数 + 退避后继续下一轮。
                consecutive_failures += 1
                await safe_incr(
                    self.worker_metrics, "worker_loop_errors_total", {"worker": "recovery"}
                )
                logger.exception(
                    "worker_round_failed",
                    extra={
                        "ctx": {
                            "worker_type": "recovery",
                            "consecutive_failures": consecutive_failures,
                        }
                    },
                )
                backoff = min(self.interval_seconds * (2 ** (consecutive_failures - 1)), 120.0)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                continue
            await safe_beat(self.worker_metrics, "recovery", "workflow-recovery")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
