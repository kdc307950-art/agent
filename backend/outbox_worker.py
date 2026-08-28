"""Outbox worker with injectable delivery adapters and a resident loop."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

import httpx

from .metrics import RuntimeMetrics
from .tickets import TicketOperationsRepository
from .worker_metrics import WorkerMetricsDB


class OutboxSender(Protocol):
    async def send(self, event: Mapping[str, Any]) -> None: ...


class TransientDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OutboxRunResult:
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0


class HttpOutboxSender:
    """Generic signed-by-idempotency HTTP sender for channel adapters."""

    def __init__(self, endpoint: str, *, shared_secret: str, timeout_seconds: float = 10.0) -> None:
        if not endpoint.startswith(("http://", "https://")) or len(shared_secret) < 16:
            raise ValueError("Outbox endpoint 或共享密钥无效")
        self.endpoint = endpoint
        self.shared_secret = shared_secret
        self.timeout = timeout_seconds

    async def send(self, event: Mapping[str, Any]) -> None:
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
            raise TransientDeliveryError(type(exc).__name__) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise TransientDeliveryError(f"HTTP_{exc.response.status_code}") from exc
            raise


class OutboxWorker:
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
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须为正数")
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            await self.run_once(limit=limit)
            if self.worker_metrics is not None:
                await self.worker_metrics.beat("outbox", self.worker_id)
                if (await self.worker_metrics.check_outbox_backlog(self.repository.pool))["dead"] > 0:
                    await self.worker_metrics.incr("outbox_dead_present_total")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _send_with_heartbeat(self, event: Mapping[str, Any], sender: OutboxSender) -> None:
        stop = asyncio.Event()
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            interval = max(0.1, self.lease_seconds / 3)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    renewed = await self.repository.renew_outbox_lease(
                        event["tenant_id"],
                        event["event_id"],
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                    if not renewed:
                        lease_lost.set()
                        break

        task = asyncio.create_task(heartbeat())
        try:
            await sender.send(event)
        finally:
            stop.set()
            await task
        if lease_lost.is_set():
            self.metrics.increment("outbox_lease_lost_total")
            raise TransientDeliveryError("lease_lost")

    async def run_once(
        self,
        *,
        limit: int = 20,
        now: datetime | None = None,
        tenant_id: str | None = None,
    ) -> OutboxRunResult:
        reference = now or datetime.now(timezone.utc)
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
                await self.repository.fail_outbox(event["tenant_id"], event["event_id"], worker_id=self.worker_id, error_code="unsupported_event_type", retry_at=None)
                dead += 1
                continue
            try:
                await self._send_with_heartbeat(event, sender)
            except TransientDeliveryError as exc:
                attempts = int(event.get("attempts", 1))
                retry_at = reference + timedelta(seconds=min(2 ** (attempts - 1), 300)) if attempts < self.max_attempts else None
                await self.repository.fail_outbox(event["tenant_id"], event["event_id"], worker_id=self.worker_id, error_code=type(exc).__name__, retry_at=retry_at)
                if retry_at is None:
                    dead += 1
                else:
                    retried += 1
            except Exception as exc:
                await self.repository.fail_outbox(event["tenant_id"], event["event_id"], worker_id=self.worker_id, error_code=type(exc).__name__, retry_at=None)
                dead += 1
            else:
                await self.repository.complete_outbox(event["tenant_id"], event["event_id"], worker_id=self.worker_id)
                delivered += 1
        self.metrics.increment("outbox_claimed_total", len(events))
        self.metrics.increment("outbox_lease_recovered_total", sum(bool(event.get("lease_recovered")) for event in events))
        self.metrics.increment("outbox_delivered_total", delivered)
        self.metrics.increment("outbox_retried_total", retried)
        self.metrics.increment("outbox_dead_total", dead)
        if self.worker_metrics is not None:
            await self.worker_metrics.incr("outbox_delivery_total", {"status": "delivered"}, delivered)
            await self.worker_metrics.incr("outbox_delivery_total", {"status": "retried"}, retried)
            await self.worker_metrics.incr("outbox_delivery_total", {"status": "dead"}, dead)
        return OutboxRunResult(len(events), delivered, retried, dead)
