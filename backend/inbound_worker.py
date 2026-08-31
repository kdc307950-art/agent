"""Inbound 入站事件 Worker —— 领取 received/failed 事件，异步建单并受理。

与 Outbox Worker 同款租约机制：FOR UPDATE SKIP LOCKED + lease 过期恢复，
多副本安全；临时错误指数退避，超过 max_attempts 进入 dead 可重放。
可观测性：心跳写 worker_heartbeats，处理结果/时延写 worker_metrics（独立进程）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .channel_adapters import NormalizedChannelEvent
from .channel_processor import process_inbound_event
from .worker_metrics import WorkerMetricsDB, safe_beat, safe_incr, safe_observe

logger = logging.getLogger(__name__)


class InboundLeaseLost(RuntimeError):
    """当前 Worker 已失去入站事件租约，不能继续产生或提交副作用。"""

    error_code = "inbound_lease_lost"


class InboundWorker:
    """入站事件异步处理 Worker（建单/受理的常驻执行者）。

    与 Outbox Worker 采用同一套租约机制：领取用 FOR UPDATE SKIP LOCKED，
    处理中持租约，超时视为失联可被其他副本回收。单条处理：
      - 成功：complete_inbound_event 标记 committed；
      - 失败：未达上限则指数退避重试，达上限进入 dead（可重放）。
    可观测性：处理结果/时延写 worker_metrics，心跳写 worker_heartbeats。
    """

    def __init__(
        self,
        runtime,
        *,
        max_attempts: int = 5,
        backoff_base_seconds: float = 30.0,
        lease_seconds: int = 120,
        batch_size: int = 20,
        tenant_id: str | None = None,
        worker_metrics: WorkerMetricsDB | None = None,
    ) -> None:
        if (
            max_attempts < 1
            or not math.isfinite(backoff_base_seconds)
            or backoff_base_seconds < 0
            or lease_seconds < 10
            or batch_size < 1
            or batch_size > 100
        ):
            raise ValueError("InboundWorker 参数无效")
        self.runtime = runtime
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self.lease_seconds = lease_seconds
        self.batch_size = batch_size
        self.tenant_id = tenant_id
        self.worker_id = f"inbound-{uuid4().hex[:8]}"
        # 为 None 时跳过打点/心跳（测试或未启用可观测性）；生产由 run_*.py 注入。
        self.worker_metrics = worker_metrics

    @staticmethod
    def _lease_key(row: dict) -> tuple[str, str, str]:
        """返回租户/渠道/外部事件复合键，避免跨渠道同 ID 串租约。"""
        return (
            str(row["tenant_id"]),
            str(row["channel"]),
            str(row["external_event_id"]),
        )

    @staticmethod
    def _event_from_row(row: dict) -> NormalizedChannelEvent:
        payload = row.get("payload") or {}
        return NormalizedChannelEvent(
            tenant_id=row["tenant_id"],
            channel=row["channel"],
            external_event_id=row["external_event_id"],
            external_ticket_id=payload.get("external_ticket_id"),
            requester_id=payload.get("requester_id") or "",
            title=payload.get("title") or "",
            content=payload.get("content") or "",
            payload=payload.get("raw") or {},
        )

    async def run_once(self) -> int:
        """领取一批并处理；返回处理条数。"""
        # 批量领取后立即为每条事件启动心跳；后排事件即使等待前排业务处理，
        # 也会持续续租，不会因串行队列等待而过期。
        rows = await self.runtime.tickets.claim_inbound_events(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            limit=self.batch_size,
            tenant_id=self.tenant_id,
        )
        lease_tasks = {
            self._lease_key(row): asyncio.create_task(self._keep_lease_alive(row))
            for row in rows
        }
        try:
            for row in rows:
                lease_task = lease_tasks[self._lease_key(row)]
                try:
                    await self._handle(row, lease_task=lease_task)
                finally:
                    lease_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await lease_task
        finally:
            for task in lease_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*lease_tasks.values(), return_exceptions=True)
        return len(rows)

    async def _keep_lease_alive(self, row: dict) -> None:
        """持续续租单个事件；续租失败立即通知业务协程停止。"""
        interval = max(0.1, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.runtime.tickets.renew_inbound_lease(
                    row["tenant_id"],
                    row["external_event_id"],
                    channel=row["channel"],
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise InboundLeaseLost() from exc
            if not renewed:
                raise InboundLeaseLost()

    async def _process_with_lease(self, row: dict, lease_task: asyncio.Task):
        """业务处理与租约监控竞速，租约失效时取消业务任务。

        业务任务完成后不取消 ``lease_task``，由调用方在 fenced complete/fail
        操作完成后再取消，确保终态写入期间租约仍受监控。
        """
        event = self._event_from_row(row)
        if lease_task.done():
            # 批量领取后排队期间可能已经失租；先消费异常再创建业务协程。
            try:
                lease_task.result()
            except asyncio.CancelledError as exc:
                raise InboundLeaseLost() from exc
            except Exception as exc:
                if isinstance(exc, InboundLeaseLost):
                    raise
                raise InboundLeaseLost() from exc
            raise InboundLeaseLost()
        work_task = asyncio.create_task(
            process_inbound_event(self.runtime, event, actor_id="inbound-worker")
        )
        try:
            done, _ = await asyncio.wait(
                {work_task, lease_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if lease_task in done:
                try:
                    lease_task.result()
                except asyncio.CancelledError as exc:
                    raise InboundLeaseLost() from exc
                except InboundLeaseLost:
                    raise
                except Exception as exc:
                    raise InboundLeaseLost() from exc
                work_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await work_task
                raise InboundLeaseLost()
            if lease_task.done() and not lease_task.cancelled():
                lease_error = lease_task.exception()
                if lease_error is not None:
                    raise InboundLeaseLost() from lease_error
            return work_task.result()
        finally:
            if not work_task.done():
                work_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work_task

    async def _handle(self, row: dict, *, lease_task: asyncio.Task | None = None) -> None:
        tenant_id = row["tenant_id"]
        event_id = row["external_event_id"]
        attempts = int(row["attempts"])
        channel = str(row["channel"])
        started = time.perf_counter()
        ticket_id: str | None = None
        error_code_log: str | None = None
        status = "lease_lost"
        owns_lease_task = lease_task is None
        if lease_task is None:
            lease_task = asyncio.create_task(self._keep_lease_alive(row))
        try:
            result = await self._process_with_lease(row, lease_task)
            ticket_id = result["ticket_id"]
            try:
                committed = await self.runtime.tickets.complete_inbound_event(
                    tenant_id,
                    event_id,
                    channel=channel,
                    ticket_id=result["ticket_id"],
                    worker_id=self.worker_id,
                )
            except Exception as exc:
                # 完成写入结果未知时不能再执行 fail（可能已经 committed），
                # 交给租约恢复/幂等重放流程处理，避免覆盖另一 Worker 的终态。
                raise InboundLeaseLost() from exc
            if not committed:
                # 另一 Worker 已接管或租约已过期；不要把本次处理报告为 committed。
                raise InboundLeaseLost()
            status = "committed"
            if result.get("resumed") is True:
                await safe_incr(self.worker_metrics, "wecom_resume_total", {"result": "resumed"})
            elif result.get("resumed") is False:
                await safe_incr(
                    self.worker_metrics,
                    "wecom_resume_total",
                    {"result": str(result.get("reason") or "noop")},
                )
        except InboundLeaseLost:
            error_code_log = InboundLeaseLost.error_code
            status = "lease_lost"
            await safe_incr(self.worker_metrics, "inbound_worker_lease_lost_total", {"channel": channel})
        except Exception as exc:
            # 失败分支：按剩余可重试次数决定进 dead 还是指数退避后重试。
            error_code = getattr(exc, "error_code", None) or type(exc).__name__
            error_code_log = error_code
            retry_at = None
            if attempts < self.max_attempts:
                # 指数退避：第 N 次失败后等待 base * 2^(N-1)，再回到 pending 队列。
                retry_at = datetime.now(UTC) + timedelta(
                    seconds=self.backoff_base_seconds * (2 ** (attempts - 1))
                )
            try:
                updated = await self.runtime.tickets.fail_inbound_event(
                    tenant_id,
                    event_id,
                    channel=channel,
                    worker_id=self.worker_id,
                    error_code=error_code,
                    retry_at=retry_at,
                )
            except Exception:
                # 无法确认 fencing 结果时保守地不计 retry/dead；租约过期后由恢复流程接管。
                logger.exception("inbound_event_failure_update_failed", extra={"event_id": event_id})
                updated = False
            if updated:
                if retry_at is None:
                    status = "dead"
                    await safe_incr(
                        self.worker_metrics, "inbound_worker_dead_total", {"channel": channel}
                    )
                else:
                    status = "failed"
                    await safe_incr(
                        self.worker_metrics, "inbound_worker_retry_total", {"channel": channel}
                    )
            else:
                status = "lease_lost"
                error_code_log = InboundLeaseLost.error_code
                await safe_incr(
                    self.worker_metrics, "inbound_worker_lease_lost_total", {"channel": channel}
                )
        finally:
            if owns_lease_task:
                lease_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await lease_task
        duration_ms = (time.perf_counter() - started) * 1000
        await safe_incr(
            self.worker_metrics, "inbound_events_total", {"channel": channel, "status": status}
        )
        await safe_observe(
            self.worker_metrics,
            "inbound_event_processing_seconds",
            duration_ms / 1000,
            {"channel": channel},
        )
        logger.info(
            "inbound_event_processed",
            extra={
                "ctx": {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "event_id": event_id,
                    "ticket_id": ticket_id,
                    "worker_id": self.worker_id,
                    "attempt": attempts,
                    "status": status,
                    "duration_ms": round(duration_ms, 1),
                    "error_code": error_code_log,
                }
            },
        )

    async def run_forever(
        self, *, poll_interval_seconds: float = 1.0, stop_event: asyncio.Event | None = None
    ) -> None:
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须为正数")
        stop_event = stop_event or asyncio.Event()
        consecutive_failures = 0
        while not stop_event.is_set():
            try:
                await self.run_once()
                consecutive_failures = 0
            except Exception:
                # 单轮失败（含 DB 领取阶段不可用）不终止常驻进程：记录 + 计数 + 退避后继续下一轮。
                consecutive_failures += 1
                await safe_incr(
                    self.worker_metrics, "worker_loop_errors_total", {"worker": "inbound"}
                )
                logger.exception(
                    "worker_round_failed",
                    extra={
                        "ctx": {
                            "worker_type": "inbound",
                            "consecutive_failures": consecutive_failures,
                        }
                    },
                )
                backoff = min(poll_interval_seconds * (2 ** (consecutive_failures - 1)), 30.0)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                continue
            await safe_beat(self.worker_metrics, "inbound", self.worker_id)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                pass
