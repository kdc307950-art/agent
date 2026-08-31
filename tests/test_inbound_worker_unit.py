"""Inbound Worker 租约 fencing 单元测试（不依赖 PostgreSQL）。"""

from __future__ import annotations

import asyncio

import backend.inbound_worker as worker_module
from backend.inbound_worker import InboundLeaseLost, InboundWorker


def _row(event_id: str = "evt-1") -> dict:
    return {
        "tenant_id": "tenant-a",
        "channel": "wecom",
        "external_event_id": event_id,
        "payload": {
            "requester_id": "u-1",
            "title": "VPN",
            "content": "cannot connect",
            "raw": {},
        },
        "attempts": 1,
    }


class _Tickets:
    def __init__(self, row: dict, *, complete: bool = True):
        self.row = row
        self.complete_result = complete
        self.completed: list[str] = []
        self.failed: list[str] = []

    async def claim_inbound_events(self, **kwargs):
        if self.row is None:
            return []
        row, self.row = self.row, None
        return [row]

    async def renew_inbound_lease(self, *args, **kwargs):
        return True

    async def complete_inbound_event(self, tenant_id, event_id, **kwargs):
        self.completed.append(event_id)
        return self.complete_result

    async def fail_inbound_event(self, tenant_id, event_id, **kwargs):
        self.failed.append(event_id)
        return True


class _Runtime:
    def __init__(self, tickets):
        self.tickets = tickets


def test_inbound_fencing_rejects_completion(monkeypatch):
    """业务处理成功但 complete fencing 失败时，不计 committed 也不再 fail 覆盖。"""
    tickets = _Tickets(_row(), complete=False)
    monkeypatch.setattr(
        worker_module,
        "process_inbound_event",
        lambda *args, **kwargs: asyncio.sleep(0, result={"ticket_id": "t-1"}),
    )
    worker = InboundWorker(_Runtime(tickets), lease_seconds=10, batch_size=1)

    assert asyncio.run(worker.run_once()) == 1
    assert tickets.completed == ["evt-1"]
    assert tickets.failed == []


def test_inbound_lease_loss_cancels_business_task(monkeypatch):
    """续租失败时取消业务协程，避免继续创建工单或提交状态。"""
    tickets = _Tickets(_row())
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_process(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(worker_module, "process_inbound_event", blocking_process)
    worker = InboundWorker(_Runtime(tickets), lease_seconds=10, batch_size=1)

    async def lose_lease(row):
        await started.wait()
        raise InboundLeaseLost()

    worker._keep_lease_alive = lose_lease
    assert asyncio.run(worker.run_once()) == 1
    assert cancelled.is_set()
    assert tickets.completed == []
    assert tickets.failed == []
