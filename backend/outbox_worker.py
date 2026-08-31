"""Outbox 投递 Worker —— 事务发件箱模式的消费端，带租约与指数退避重试。

职责：
    - 周期性地从 outbox_events 表领取待投递事件并调用对应 sender 投递
    - 支持多个常驻 Worker 进程并发消费（数据库租约互斥）
    - 区分可重试的瞬时错误（TransientDeliveryError）与致命错误（死信）

关键设计：
    - 租约机制：claim_outbox 用 worker_id + lease_seconds 抢占事件，
      投递期间由心跳协程续租（renew_outbox_lease），租约丢失立即中止
    - 幂等投递：HttpOutboxSender 为每次投递生成时间戳 + HMAC-SHA256 签名，
      并以 tenant_id:idempotency_key 作为 X-Idempotency-Key，下游可去重
    - 退避重试：瞬时失败按 attempt 指数退避（2^n 秒封顶 300 秒），
      超过 max_attempts 或致命错误进入死信（dead）状态
    - 常驻循环：单轮失败只记录并退避，不终止进程；心跳与死信水位
      通过 worker_metrics 上报
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

import httpx

from .metrics import RuntimeMetrics
from .tickets import TicketOperationsRepository
from .worker_metrics import WorkerMetricsDB, safe_beat, safe_incr

logger = logging.getLogger(__name__)


class OutboxSender(Protocol):
    """Outbox 事件投递器协议：实现 send(event) 把事件送到目标系统。

    实现方按 event["event_type"] 路由选择；send 抛 TransientDeliveryError
    表示瞬时失败（可重试），抛其它异常表示致命失败（进死信）。
    """

    async def send(self, event: Mapping[str, Any]) -> None: ...


class TransientDeliveryError(RuntimeError):
    """瞬时投递错误（网络超时、5xx、租约丢失等），可退避重试。"""

    pass


class OutboxLeaseLost(TransientDeliveryError):
    """投递期间 Worker 失去事件租约。"""

    error_code = "outbox_lease_lost"


@dataclass(frozen=True, slots=True)
class OutboxRunResult:
    """单轮投递结果统计（不可变快照，供测试断言与指标上报）。

    claimed：本轮领取的事件数；delivered：成功投递数；
    retried：瞬时失败已排重试数；dead：进入死信数；lease_lost：租约 fencing
    失败或续租异常数（不代表事件已进入终态）。
    """

    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0
    lease_lost: int = 0


class HttpOutboxSender:
    """Generic signed-by-idempotency HTTP sender for channel adapters.

    中文说明：面向渠道适配器的通用 HTTP 投递器。
    每次投递携带时间戳签名（HMAC-SHA256，请求头 X-Outbox-Signature）与
    幂等键（X-Idempotency-Key = tenant_id:idempotency_key），使下游接收方
    可以校验来源并去重；408/409/425/429/5xx 视为瞬时错误可重试。
    """

    def __init__(self, endpoint: str, *, shared_secret: str, timeout_seconds: float = 10.0) -> None:
        if (
            not endpoint.startswith(("http://", "https://"))
            or len(shared_secret) < 16
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            # 拒绝非 HTTP 端点与过短密钥，避免误配置把事件发到任意协议。
            raise ValueError("Outbox endpoint 或共享密钥无效")
        self.endpoint = endpoint
        self.shared_secret = shared_secret
        self.timeout = timeout_seconds

    async def send(self, event: Mapping[str, Any]) -> None:
        """POST 事件 payload 到配置端点，失败按类型抛异常。

        抛错：TransientDeliveryError —— 超时/网络错误/可重试 HTTP 状态码；
            其它 httpx.HTTPStatusError（4xx 等）原样上抛，视为致命错误。
        设计：body 为紧凑 JSON（sort_keys 保证签名可复现），签名覆盖
        "timestamp.body"，防篡改与重放。
        """
        body = json.dumps(event["payload"], separators=(",", ":"), sort_keys=True).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.shared_secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Idempotency-Key": f"{event['tenant_id']}:{event['idempotency_key']}",
                        "X-Outbox-Timestamp": timestamp,
                        "X-Outbox-Signature": f"sha256={signature}",
                    },
                )
                response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # 网络层失败：目标可能临时不可达，按瞬时错误处理。
            raise TransientDeliveryError(type(exc).__name__) from exc
        except httpx.HTTPStatusError as exc:
            # 408 请求超时 / 409 冲突 / 425 过早 / 429 限流 / 5xx 服务端错误
            # 都说明「重试可能成功」；其余 4xx（参数错误等）重试无意义。
            if exc.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise TransientDeliveryError(f"HTTP_{exc.response.status_code}") from exc
            raise


class OutboxWorker:
    """Outbox 消费 Worker：领取事件 → 投递（带心跳续租）→ 统计/上报。

    构造参数：repository 提供 claim/fail/complete/renew 等操作；
    senders 为 event_type → OutboxSender 路由表；max_attempts 最大尝试次数；
    lease_seconds 租约时长；worker_id 本实例唯一 ID（未传则随机生成）；
    metrics/worker_metrics 为可选的指标上报通道。
    """

    def __init__(
        self,
        repository: TicketOperationsRepository,
        senders: Mapping[str, OutboxSender],
        *,
        max_attempts: int = 5,
        lease_seconds: int = 60,
        worker_id: str | None = None,
        metrics: RuntimeMetrics | None = None,
        worker_metrics: WorkerMetricsDB | None = None,
    ) -> None:
        if max_attempts < 1 or lease_seconds < 1:
            raise ValueError("Worker 参数必须为正数")
        self.repository = repository
        self.senders = dict(senders)
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        # 无显式 worker_id 时生成随机 hex：多实例并发时靠它区分租约归属。
        self.worker_id = worker_id or uuid4().hex
        self.metrics = metrics or RuntimeMetrics(service_name="helpdesk-outbox-worker")
        # 为 None 时跳过 DB 打点/心跳（测试或未启用可观测性）。
        self.worker_metrics = worker_metrics

    @staticmethod
    def _lease_key(event: Mapping[str, Any]) -> tuple[str, str]:
        """返回租户/事件复合键，避免跨租户同 event_id 覆盖心跳任务。"""
        return (str(event["tenant_id"]), str(event["event_id"]))

    async def run_forever(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        limit: int = 20,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """常驻循环：轮询执行 run_once，直到 stop_event 置位。

        参数：poll_interval_seconds 轮询间隔（默认 1 秒）；limit 每轮领取上限；
            stop_event 停止信号（收到 SIGINT/SIGTERM 时由入口设置）。
        设计：单轮异常不退出进程——计数 + 指数退避（封顶 30 秒）后继续；
        每轮结束后上报心跳与死信水位指标。
        """
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须为正数")
        stop_event = stop_event or asyncio.Event()
        consecutive_failures = 0
        while not stop_event.is_set():
            try:
                await self.run_once(limit=limit)
                consecutive_failures = 0
            except Exception:
                # 单轮失败（含 DB 领取阶段不可用）不终止常驻进程：记录 + 计数 + 退避后继续下一轮。
                consecutive_failures += 1
                await safe_incr(
                    self.worker_metrics, "worker_loop_errors_total", {"worker": "outbox"}
                )
                logger.exception(
                    "worker_round_failed",
                    extra={
                        "ctx": {
                            "worker_type": "outbox",
                            "consecutive_failures": consecutive_failures,
                        }
                    },
                )
                backoff = min(poll_interval_seconds * (2 ** (consecutive_failures - 1)), 30.0)
                try:
                    # 退避期间可被 stop_event 提前唤醒，保证停机响应及时。
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                continue
            await safe_beat(self.worker_metrics, "outbox", self.worker_id)
            if self.worker_metrics is not None:
                try:
                    # 每轮顺带查询死信水位：>0 时上报 outbox_dead_present_total。
                    dead = (await self.worker_metrics.check_outbox_backlog(self.repository.pool))[
                        "dead"
                    ]
                except Exception:
                    # 查询失败时不假设 dead=0：故障期间死信指标不得假 0，只记录错误计数。
                    await safe_incr(self.worker_metrics, "outbox_backlog_check_errors_total")
                    dead = None
                if dead is not None and dead > 0:
                    await safe_incr(self.worker_metrics, "outbox_dead_present_total")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                pass

    async def _send_with_heartbeat(self, event: Mapping[str, Any], sender: OutboxSender) -> None:
        """投递单个事件，同时后台心跳续租，租约丢失则中止投递。

        设计：心跳间隔 = lease_seconds / 3（保证租约过期前至少续 3 次）；
        续租失败立即取消投递任务，避免本 worker 已失去所有权后继续产生副作用。
        """
        lease_task = asyncio.create_task(self._keep_lease_alive(event))
        try:
            await self._send_with_existing_lease(event, sender, lease_task)
        finally:
            if not lease_task.done():
                lease_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await lease_task

    async def _keep_lease_alive(self, event: Mapping[str, Any]) -> None:
        """续租单个已领取事件；异常或 False 结果都表示 fencing 失败。"""
        interval = max(0.1, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.repository.renew_outbox_lease(
                    event["tenant_id"],
                    event["event_id"],
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise OutboxLeaseLost("outbox_lease_lost") from exc
            if not renewed:
                raise OutboxLeaseLost("outbox_lease_lost") from None

    async def _fenced_complete(self, event: Mapping[str, Any]) -> bool:
        """完成写入失败时保守返回 False，不让单条 DB 故障中断整批处理。"""
        try:
            return await self.repository.complete_outbox(
                event["tenant_id"], event["event_id"], worker_id=self.worker_id
            )
        except Exception:
            logger.exception(
                "outbox_complete_failed",
                extra={"tenant_id": event.get("tenant_id"), "event_id": event.get("event_id")},
            )
            return False

    async def _fenced_fail(
        self,
        event: Mapping[str, Any],
        *,
        error_code: str,
        retry_at: datetime | None,
    ) -> bool:
        """失败状态写入采用 fencing；DB 异常按失租处理并继续下一事件。"""
        try:
            return await self.repository.fail_outbox(
                event["tenant_id"],
                event["event_id"],
                worker_id=self.worker_id,
                error_code=error_code,
                retry_at=retry_at,
            )
        except Exception:
            logger.exception(
                "outbox_failure_update_failed",
                extra={"tenant_id": event.get("tenant_id"), "event_id": event.get("event_id")},
            )
            return False

    async def _send_with_existing_lease(
        self,
        event: Mapping[str, Any],
        sender: OutboxSender,
        lease_task: asyncio.Task,
    ) -> None:
        """使用调用方从 claim 时启动的续租任务执行投递。"""
        if lease_task.done():
            # 事件可能在批内等待期间已失租；检查后再创建 sender，避免旧 owner
            # 在取消协程前向下游发出请求。
            try:
                lease_task.result()
            except asyncio.CancelledError as exc:
                raise OutboxLeaseLost("outbox_lease_lost") from exc
            except Exception as exc:
                if isinstance(exc, OutboxLeaseLost):
                    raise
                raise OutboxLeaseLost("outbox_lease_lost") from exc
            raise OutboxLeaseLost("outbox_lease_lost")
        sender_task = asyncio.create_task(sender.send(event))
        try:
            done, _ = await asyncio.wait(
                {sender_task, lease_task}, return_when=asyncio.FIRST_COMPLETED
            )
            # 同一调度点两个任务都可能完成；租约失败优先，避免把失租投递报成功。
            if lease_task in done:
                try:
                    lease_task.result()
                except asyncio.CancelledError:
                    raise
                except OutboxLeaseLost:
                    sender_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await sender_task
                    raise
                except Exception as exc:
                    sender_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await sender_task
                    raise OutboxLeaseLost("outbox_lease_lost") from exc

            if sender_task in done:
                # 发送完成与心跳失败同时发生时仍以失租为准。
                if lease_task.done():
                    try:
                        lease_task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if isinstance(exc, OutboxLeaseLost):
                            raise
                        raise OutboxLeaseLost("outbox_lease_lost") from exc
                sender_task.result()
                return

            raise OutboxLeaseLost("outbox_lease_lost")
        finally:
            if not sender_task.done():
                sender_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await sender_task

    async def run_once(
        self,
        *,
        limit: int = 20,
        now: datetime | None = None,
        tenant_id: str | None = None,
    ) -> OutboxRunResult:
        """执行一轮领取与投递，返回统计结果。

        参数：limit 领取上限；now 注入当前时间（测试用，影响退避计算）；
            tenant_id 限定只处理某租户的事件（测试/定向重放用）。
        返回：OutboxRunResult(claimed, delivered, retried, dead, lease_lost)。
        设计：单事件隔离失败——一个事件投递失败不影响同批其它事件；
        按 event_type 路由 sender，路由缺失直接判死信（unsupported_event_type）。
        """
        reference = now or datetime.now(UTC)
        if limit < 1 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        # 批量领取后立即为每条事件启动心跳；后排事件即使等待前排发送，也会持续续租。
        events = await self.repository.claim_outbox(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            limit=limit,
            tenant_id=tenant_id,
        )
        delivered = retried = dead = lease_lost = 0
        lease_tasks = {
            self._lease_key(event): asyncio.create_task(self._keep_lease_alive(event))
            for event in events
        }
        try:
            for event in events:
                lease_task = lease_tasks[self._lease_key(event)]
                sender = self.senders.get(str(event["event_type"]))
                if sender is None:
                    # 未注册的 event_type：无法投递且重试无意义，直接进死信。
                    updated = await self._fenced_fail(
                        event,
                        error_code="unsupported_event_type",
                        retry_at=None,
                    )
                    if updated:
                        dead += 1
                    else:
                        lease_lost += 1
                    lease_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await lease_task
                    continue
                try:
                    await self._send_with_existing_lease(event, sender, lease_task)
                except OutboxLeaseLost:
                    # 租约已丢失：不要再调用 fail_outbox（旧 owner 无权改状态），
                    # 让事件自然过期后由恢复 Worker 重新领取。
                    lease_lost += 1
                except TransientDeliveryError as exc:
                    attempts = int(event.get("attempts", 1))
                    retry_at = (
                        reference + timedelta(seconds=min(2 ** (attempts - 1), 300))
                        if attempts < self.max_attempts
                        else None
                    )
                    updated = await self._fenced_fail(
                        event,
                        error_code=getattr(exc, "error_code", type(exc).__name__),
                        retry_at=retry_at,
                    )
                    if not updated:
                        lease_lost += 1
                    elif retry_at is None:
                        dead += 1
                    else:
                        retried += 1
                except Exception as exc:
                    # 非 TransientDeliveryError 的异常：视为致命失败，直接进死信。
                    updated = await self._fenced_fail(
                        event,
                        error_code=getattr(exc, "error_code", type(exc).__name__),
                        retry_at=None,
                    )
                    if updated:
                        dead += 1
                    else:
                        lease_lost += 1
                else:
                    # 只有仍持租约并成功写入终态，才报告 delivered。
                    completed = await self._fenced_complete(event)
                    if completed:
                        delivered += 1
                    else:
                        lease_lost += 1
                finally:
                    lease_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await lease_task
        finally:
            for task in lease_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*lease_tasks.values(), return_exceptions=True)
        # 双通道指标上报：RuntimeMetrics（进程内）+ WorkerMetricsDB（数据库）。
        self.metrics.increment("outbox_claimed_total", len(events))
        self.metrics.increment(
            "outbox_lease_recovered_total",
            sum(bool(event.get("lease_recovered")) for event in events),
        )
        self.metrics.increment("outbox_delivered_total", delivered)
        self.metrics.increment("outbox_retried_total", retried)
        self.metrics.increment("outbox_dead_total", dead)
        self.metrics.increment("outbox_lease_lost_total", lease_lost)
        await safe_incr(self.worker_metrics, "outbox_claimed_total", amount=len(events))
        await safe_incr(
            self.worker_metrics,
            "outbox_lease_recovered_total",
            amount=sum(bool(event.get("lease_recovered")) for event in events),
        )
        await safe_incr(self.worker_metrics, "outbox_delivered_total", amount=delivered)
        await safe_incr(self.worker_metrics, "outbox_retried_total", amount=retried)
        await safe_incr(self.worker_metrics, "outbox_dead_total", amount=dead)
        await safe_incr(self.worker_metrics, "outbox_lease_lost_total", amount=lease_lost)
        return OutboxRunResult(len(events), delivered, retried, dead, lease_lost)
