"""常驻 SLA 扫描 Worker —— 检测超时工单并产生幂等的 Outbox 升级事件。

职责：
    - 周期性调用 scan_sla_breaches 找出超过 SLA 时限未响应的工单
    - 为每个违约工单写入一条 sla.breached Outbox 事件，交给 Outbox Worker
      异步投递（邮件/企微/钉钉等渠道通知）

关键设计：
    - 事件幂等：扫描结果依赖数据库去重/幂等约束，重复扫描同一工单
      不会重复建单，多实例并发扫描也安全
    - 常驻循环：单轮失败只记录 + 计数 + 指数退避（封顶 120 秒），不终止进程
    - 指标上报：sla_scan_runs_total / sla_breach_events_total / 心跳，
      支持 WorkerMetricsDB 落库与 RuntimeMetrics 进程内计数
"""

from __future__ import annotations

import asyncio
import logging

from .metrics import RuntimeMetrics
from .tickets import TicketOperationsRepository
from .worker_metrics import WorkerMetricsDB, safe_beat, safe_incr

logger = logging.getLogger(__name__)


class SLAWorker:
    """SLA 违约扫描 Worker。

    构造参数：repository 提供 scan_sla_breaches；interval_seconds 扫描间隔
    （默认 30 秒）；batch_size 每轮最多处理的违约数（1~1000）；
    metrics/worker_metrics 为可选指标通道（为 None 时跳过 DB 打点）。
    """

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
        """执行一轮 SLA 扫描，返回本轮产生的违约事件数。

        返回：int —— 本轮新建的 SLA 违约事件数量。
        抛错：扫描异常原样上抛（由 run_forever 捕获退避），同时上报
        sla_scan_errors_total 指标。
        """
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
        """常驻循环：按 interval_seconds 周期执行 SLA 扫描，直到收到停止信号。

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
