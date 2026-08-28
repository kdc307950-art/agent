"""InboundWorker 生命周期集成测试 —— 快速 ACK 后的异步建单受理、失败重试、dead 与重放。

覆盖：received → processing → committed；临时失败退避重试；超过次数 dead；
dead 可 replay；Worker 崩溃后租约过期可恢复。
"""

import asyncio
import os
from uuid import uuid4

import pytest

from backend.inbound_worker import InboundWorker
from backend.migrations import setup_postgres
from backend.runtime import runtime_context
from backend.seed_demo import _seed
from backend.settings import Settings


DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def _event_payload():
    return {
        "requester_id": "ext-user-1",
        "external_ticket_id": None,
        "title": "VPN 无法连接",
        "content": "VPN 无法连接，错误码 809",
        "channel": "wecom",
        "raw": {},
    }


def test_inbound_worker_creates_ticket_and_commits(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    event_id = f"evt-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            await _seed(tenant, DATABASE_URL)
            await runtime.tickets.register_inbound_event(tenant, "wecom", event_id, _event_payload())
            worker = InboundWorker(runtime, batch_size=10, tenant_id=tenant)
            processed = await worker.run_once()
            event = await runtime.tickets.get_inbound_event(tenant, "wecom", event_id)
            ticket = await runtime.tickets.get(tenant, event["ticket_id"])
            return processed, event, ticket

    processed, event, ticket = asyncio.run(run())
    assert processed == 1
    assert event["status"] == "committed"
    assert event["ticket_id"] is not None
    assert ticket is not None
    assert ticket.title == "VPN 无法连接"
    # 渠道消息缺 it.vpn 策略必填字段 → 追问（awaiting_customer），分类在补字段后完成。
    assert ticket.status == "awaiting_customer"
    assert ticket.category is None


def test_inbound_worker_retries_then_dead_letters_and_replays(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    event_id = f"evt-{uuid4().hex}"
    state = {"calls": 0}

    async def failing_process(runtime, event, *, actor_id):
        state["calls"] += 1
        if state["calls"] < 4:
            raise RuntimeError("transient boom")
        from backend.channel_processor import process_inbound_event as real

        return await real(runtime, event, actor_id=actor_id)

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            await _seed(tenant, DATABASE_URL)
            await runtime.tickets.register_inbound_event(tenant, "wecom", event_id, _event_payload())
            import backend.inbound_worker as worker_module

            monkeypatch.setattr(worker_module, "process_inbound_event", failing_process)
            worker = InboundWorker(runtime, max_attempts=3, backoff_base_seconds=0, batch_size=10, tenant_id=tenant)
            await worker.run_once()  # 第 1 次失败 -> failed（可重试）
            event = await runtime.tickets.get_inbound_event(tenant, "wecom", event_id)
            first_status = event["status"]
            first_attempts = event["attempts"]
            await worker.run_once()  # 第 2 次失败 -> failed
            event = await runtime.tickets.get_inbound_event(tenant, "wecom", event_id)
            second_status = event["status"]
            await worker.run_once()  # 第 3 次失败 -> 达到 max_attempts -> dead
            event = await runtime.tickets.get_inbound_event(tenant, "wecom", event_id)
            dead_status = event["status"]
            replayed = await runtime.tickets.replay_inbound_event(tenant, event_id)
            event = await runtime.tickets.get_inbound_event(tenant, "wecom", event_id)
            replayed_status = event["status"]
            await worker.run_once()  # 第 4 次成功 -> committed
            event = await runtime.tickets.get_inbound_event(tenant, "wecom", event_id)
            return first_status, first_attempts, second_status, dead_status, replayed, replayed_status, event["status"], event["ticket_id"]

    first, first_attempts, second, dead, replayed, replayed_status, final, ticket_id = asyncio.run(run())
    assert first == "failed" and first_attempts == 1
    assert second == "failed"
    assert dead == "dead"
    assert replayed is True
    assert replayed_status == "received"
    assert final == "committed"
    assert ticket_id is not None


def test_inbound_worker_recovers_expired_lease(monkeypatch):
    """Worker 崩溃后租约过期：事件可被再次领取并成功处理（不重复建单）。"""
    tenant = f"tenant-{uuid4().hex}"
    event_id = f"evt-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            await _seed(tenant, DATABASE_URL)
            await runtime.tickets.register_inbound_event(tenant, "wecom", event_id, _event_payload())
            # 模拟上次 Worker 崩溃：事件卡在 processing 且租约已过期。
            async with runtime.tickets.pool.connection() as connection:
                await connection.execute(
                    """
                    UPDATE inbound_events SET status = 'processing',
                        worker_id = 'crashed-worker', lease_expires_at = now() - interval '1 second',
                        attempts = 1
                    WHERE tenant_id = %s AND external_event_id = %s
                    """,
                    (tenant, event_id),
                )
            worker = InboundWorker(runtime, batch_size=10, tenant_id=tenant)
            processed = await worker.run_once()
            event = await runtime.tickets.get_inbound_event(tenant, "wecom", event_id)
            ticket = await runtime.tickets.get(tenant, event["ticket_id"])
            return processed, event["status"], ticket is not None

    processed, status, has_ticket = asyncio.run(run())
    assert processed == 1
    assert status == "committed"
    assert has_ticket is True
