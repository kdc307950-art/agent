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
import time
from collections.abc import Mapping
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


@dataclass(frozen=True, slots=True)
class OutboxRunResult:
    """单轮投递结果统计（不可变快照，供测试断言与指标上报）。

    claimed：本轮领取的事件数；delivered：成功投递数；
    retried：瞬时失败已排重试数；dead：进入死信数。
    """

    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0


class HttpOutboxSender:
    """Generic signed-by-idempotency HTTP sender for channel adapters.

    中文说明：面向渠道适配器的通用 HTTP 投递器。
    每次投递携带时间戳签名（HMAC-SHA256，请求头 X-Outbox-Signature）与
    幂等键（X-Idempotency-Key = tenant_id:idempotency_key），使下游接收方
    可以校验来源并去重；408/409/425/429/5xx 视为瞬时错误可重试。
    """

    def __init__(self, endpoint: str, *, shared_secret: str, timeout_seconds: float = 10.0) -> None:
        if not endpoint.startswith(("http://", "https://")) or len(shared_secret) < 16:
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
        if poll_interval_seconds <= 0:
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
        续租失败置 lease_lost，投递结束后据此抛 TransientDeliveryError，
        避免本 worker 已失去所有权后还继续占用事件。
        """
        stop = asyncio.Event()
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            interval = max(0.1, self.lease_seconds / 3)
            while not stop.is_set():
                try:
                    # 每隔 interval 尝试续租一次；stop 置位即退出协程。
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    break
                except TimeoutError:
                    renewed = await self.repository.renew_outbox_lease(
                        event["tenant_id"],
                        event["event_id"],
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                    if not renewed:
                        # 租约已被其它 worker 抢走或已过期：标记并退出心跳。
                        lease_lost.set()
                        break

        task = asyncio.create_task(heartbeat())
        try:
            await sender.send(event)
        finally:
            # 无论成败都停止心跳协程，避免任务泄漏。
            stop.set()
            await task
        if lease_lost.is_set():
            # 投递期间租约丢失：事件可能已被他人重新领取，按瞬时错误处理
            # 让本事件重新进入调度（fail_outbox 会走重试/死信逻辑）。
            self.metrics.increment("outbox_lease_lost_total")
            raise TransientDeliveryError("lease_lost")

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
        返回：OutboxRunResult(claimed, delivered, retried, dead)。
        设计：单事件隔离失败——一个事件投递失败不影响同批其它事件；
        按 event_type 路由 sender，路由缺失直接判死信（unsupported_event_type）。
        """
        reference = now or datetime.now(UTC)
        events = await self.repository.claim_outbox(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            limit=limit,
            tenant_id=tenant_id,
        )
        delivered = retried = dead = 0
        for event in events:
            sender = self.senders.get(str(event["event_type"]))
            if sender is None:
                # 未注册的 event_type：无法投递且重试无意义，直接进死信。
                await self.repository.fail_outbox(
                    event["tenant_id"],
                    event["event_id"],
                    worker_id=self.worker_id,
                    error_code="unsupported_event_type",
                    retry_at=None,
                )
                dead += 1
                continue
            try:
                await self._send_with_heartbeat(event, sender)
            except TransientDeliveryError as exc:
                attempts = int(event.get("attempts", 1))
                # 指数退避：第 n 次失败后等待 2^(n-1) 秒，封顶 300 秒；
                # 已达 max_attempts 则不再排重试（retry_at=None → 死信）。
                retry_at = (
                    reference + timedelta(seconds=min(2 ** (attempts - 1), 300))
                    if attempts < self.max_attempts
                    else None
                )
                await self.repository.fail_outbox(
                    event["tenant_id"],
                    event["event_id"],
                    worker_id=self.worker_id,
                    error_code=type(exc).__name__,
                    retry_at=retry_at,
                )
                if retry_at is None:
                    dead += 1
                else:
                    retried += 1
            except Exception as exc:
                # 非 TransientDeliveryError 的异常：视为致命失败，直接进死信。
                await self.repository.fail_outbox(
                    event["tenant_id"],
                    event["event_id"],
                    worker_id=self.worker_id,
                    error_code=type(exc).__name__,
                    retry_at=None,
                )
                dead += 1
            else:
                # 投递成功：标记完成（complete_outbox），从待投递集合移除。
                await self.repository.complete_outbox(
                    event["tenant_id"], event["event_id"], worker_id=self.worker_id
                )
                delivered += 1
        # 双通道指标上报：RuntimeMetrics（进程内）+ WorkerMetricsDB（数据库）。
        self.metrics.increment("outbox_claimed_total", len(events))
        self.metrics.increment(
            "outbox_lease_recovered_total",
            sum(bool(event.get("lease_recovered")) for event in events),
        )
        self.metrics.increment("outbox_delivered_total", delivered)
        self.metrics.increment("outbox_retried_total", retried)
        self.metrics.increment("outbox_dead_total", dead)
        await safe_incr(self.worker_metrics, "outbox_claimed_total", amount=len(events))
        await safe_incr(
            self.worker_metrics,
            "outbox_lease_recovered_total",
            amount=sum(bool(event.get("lease_recovered")) for event in events),
        )
        await safe_incr(self.worker_metrics, "outbox_delivered_total", amount=delivered)
        await safe_incr(self.worker_metrics, "outbox_retried_total", amount=retried)
        await safe_incr(self.worker_metrics, "outbox_dead_total", amount=dead)
        return OutboxRunResult(len(events), delivered, retried, dead)
