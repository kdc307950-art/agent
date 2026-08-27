"""Outbox worker with injectable delivery adapters and a resident loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

import httpx

from .metrics import RuntimeMetrics
from .tickets import TicketOperationsRepository


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

    def __init__(self, endpoint: str, *, timeout_seconds: float = 10.0) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("Outbox endpoint 必须是 http(s) URL")
        self.endpoint = endpoint
        self.timeout = timeout_seconds

    async def send(self, event: Mapping[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint,
                    json=event["payload"],
                    headers={"X-Idempotency-Key": f"{event['tenant_id']}:{event['idempotency_key']}"},
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
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须为正数")
        self.repository = repository
        self.senders = dict(senders)
        self.max_attempts = max_attempts
        self.metrics = metrics or RuntimeMetrics(service_name="helpdesk-outbox-worker")

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
            result = await self.run_once(limit=limit)
            if result.claimed == 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass

    async def run_once(
        self,
        *,
        limit: int = 20,
        now: datetime | None = None,
        tenant_id: str | None = None,
    ) -> OutboxRunResult:
        reference = now or datetime.now(timezone.utc)
        events = await self.repository.claim_outbox(limit=limit, tenant_id=tenant_id)
        delivered = retried = dead = 0
        for event in events:
            sender = self.senders.get(str(event["event_type"]))
            if sender is None:
                await self.repository.fail_outbox(event["tenant_id"], event["event_id"], error_code="unsupported_event_type", retry_at=None)
                dead += 1
                continue
            try:
                await sender.send(event)
            except TransientDeliveryError as exc:
                attempts = int(event.get("attempts", 1))
                retry_at = reference + timedelta(seconds=min(2 ** (attempts - 1), 300)) if attempts < self.max_attempts else None
                await self.repository.fail_outbox(event["tenant_id"], event["event_id"], error_code=type(exc).__name__, retry_at=retry_at)
                if retry_at is None:
                    dead += 1
                else:
                    retried += 1
            except Exception as exc:
                await self.repository.fail_outbox(event["tenant_id"], event["event_id"], error_code=type(exc).__name__, retry_at=None)
                dead += 1
            else:
                await self.repository.complete_outbox(event["tenant_id"], event["event_id"])
                delivered += 1
        self.metrics.increment("outbox_claimed_total", len(events))
        self.metrics.increment("outbox_delivered_total", delivered)
        self.metrics.increment("outbox_retried_total", retried)
        self.metrics.increment("outbox_dead_total", dead)
        return OutboxRunResult(len(events), delivered, retried, dead)
