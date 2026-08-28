"""Inbound 入站事件 Worker —— 领取 received/failed 事件，异步建单并受理。

与 Outbox Worker 同款租约机制：FOR UPDATE SKIP LOCKED + lease 过期恢复，
多副本安全；临时错误指数退避，超过 max_attempts 进入 dead 可重放。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .channel_adapters import NormalizedChannelEvent
from .channel_processor import process_inbound_event


class InboundWorker:
    def __init__(
        self,
        runtime,
        *,
        max_attempts: int = 5,
        backoff_base_seconds: float = 30.0,
        lease_seconds: int = 120,
        batch_size: int = 20,
        tenant_id: str | None = None,
    ) -> None:
        if max_attempts < 1 or backoff_base_seconds < 0 or lease_seconds < 10:
            raise ValueError("InboundWorker 参数无效")
        self.runtime = runtime
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self.lease_seconds = lease_seconds
        self.batch_size = batch_size
        self.tenant_id = tenant_id
        self.worker_id = f"inbound-{uuid4().hex[:8]}"

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
        rows = await self.runtime.tickets.claim_inbound_events(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            limit=self.batch_size,
            tenant_id=self.tenant_id,
        )
        for row in rows:
            await self._handle(row)
        return len(rows)

    async def _handle(self, row: dict) -> None:
        tenant_id = row["tenant_id"]
        event_id = row["external_event_id"]
        attempts = int(row["attempts"])
        try:
            event = self._event_from_row(row)
            result = await process_inbound_event(self.runtime, event, actor_id="inbound-worker")
            await self.runtime.tickets.complete_inbound_event(
                tenant_id,
                event_id,
                ticket_id=result["ticket_id"],
                worker_id=self.worker_id,
            )
        except Exception as exc:
            error_code = getattr(exc, "error_code", None) or type(exc).__name__
            if attempts >= self.max_attempts:
                await self.runtime.tickets.fail_inbound_event(
                    tenant_id, event_id, worker_id=self.worker_id, error_code=error_code, retry_at=None
                )
            else:
                retry_at = datetime.now(timezone.utc) + timedelta(
                    seconds=self.backoff_base_seconds * (2 ** (attempts - 1))
                )
                await self.runtime.tickets.fail_inbound_event(
                    tenant_id, event_id, worker_id=self.worker_id, error_code=error_code, retry_at=retry_at
                )

    async def run_forever(self, *, poll_interval_seconds: float = 1.0, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
