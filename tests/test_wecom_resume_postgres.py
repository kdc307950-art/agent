"""企微追问 Resume 闭环集成测试。

覆盖：渠道工单追问 → 登记待补全 → 客户回复按字段恢复原工单（绝不新建）→
分类/SLA/派单；同 MsgId 幂等；无待补全时新建；过期待补全不 resume。
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.inbound_worker import InboundWorker
from backend.migrations import setup_postgres
from backend.runtime import runtime_context
from backend.seed_demo import _seed
from backend.settings import Settings
from src.my_agent.helpdesk import TicketStatus

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def _payload(content: str, requester: str = "ext-user-1"):
    return {
        "requester_id": requester,
        "external_ticket_id": None,
        "title": content[:40],
        "content": content,
        "channel": "wecom",
        "raw": {},
    }


FULL_REPLY = (
    "affected_system: 公司 VPN\ndevice: laptop-001\noperating_system: Windows 11\n"
    "error_message: 809\nimpact: 无法远程办公\nnetwork: 办公网"
)


async def _count_tickets(runtime, tenant: str) -> int:
    return len(await runtime.tickets.list_tickets(tenant, limit=100))


def test_wecom_reply_resumes_original_ticket_not_new_one(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            await _seed(tenant, DATABASE_URL)
            worker = InboundWorker(runtime, batch_size=10, tenant_id=tenant)
            # 1) 首次消息：建单 + 追问，登记待补全
            first_event = f"evt-{uuid4().hex}"
            await runtime.tickets.register_inbound_event(
                tenant, "wecom", first_event, _payload("VPN 无法连接，错误码 809")
            )
            await worker.run_once()
            first_ticket = await runtime.tickets.get_inbound_event(tenant, "wecom", first_event)
            ticket = await runtime.tickets.get(tenant, first_ticket["ticket_id"])
            pending = await runtime.tickets.get_pending_intake(tenant, ticket.ticket_id)
            # 2) 客户回复补字段：恢复原工单
            reply_event = f"evt-{uuid4().hex}"
            await runtime.tickets.register_inbound_event(
                tenant, "wecom", reply_event, _payload(FULL_REPLY)
            )
            await worker.run_once()
            reply_record = await runtime.tickets.get_inbound_event(tenant, "wecom", reply_event)
            ticket_after = await runtime.tickets.get(tenant, ticket.ticket_id)
            pending_after = await runtime.tickets.get_pending_intake(tenant, ticket.ticket_id)
            total = await _count_tickets(runtime, tenant)
            return first_ticket, ticket, pending, reply_record, ticket_after, pending_after, total

    first_ticket, ticket, pending, reply_record, ticket_after, pending_after, total = asyncio.run(
        run()
    )
    assert first_ticket["status"] == "committed"
    assert ticket.status == "awaiting_customer"
    assert pending is not None and pending["status"] == "awaiting"
    assert reply_record["status"] == "committed"
    # 绝不新建工单：仍只有 1 张工单，且回复关联的是原工单
    assert total == 1
    assert reply_record["ticket_id"] == ticket.ticket_id
    assert ticket_after.ticket_id == ticket.ticket_id
    assert ticket_after.category == "it.vpn"
    assert ticket_after.status in {TicketStatus.QUEUED, TicketStatus.ASSIGNED}
    assert pending_after["status"] == "resumed"
    assert pending_after["resume_count"] == 1


def test_wecom_reply_idempotent_same_msgid(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            await _seed(tenant, DATABASE_URL)
            worker = InboundWorker(runtime, batch_size=10, tenant_id=tenant)
            first_event = f"evt-{uuid4().hex}"
            await runtime.tickets.register_inbound_event(
                tenant, "wecom", first_event, _payload("VPN 无法连接，错误码 809")
            )
            await worker.run_once()
            ticket = await runtime.tickets.get(
                tenant,
                (await runtime.tickets.get_inbound_event(tenant, "wecom", first_event))[
                    "ticket_id"
                ],
            )
            # 同一条回复 MsgId 重放：第二次登记返回 created=False，worker 不重复处理
            reply_event = f"evt-{uuid4().hex}"
            await runtime.tickets.register_inbound_event(
                tenant, "wecom", reply_event, _payload(FULL_REPLY)
            )
            second_reg = await runtime.tickets.register_inbound_event(
                tenant, "wecom", reply_event, _payload(FULL_REPLY)
            )
            await worker.run_once()
            total = await _count_tickets(runtime, tenant)
            return (
                second_reg.created,
                total,
                (await runtime.tickets.get(tenant, ticket.ticket_id)).status,
            )

    created, total, status = asyncio.run(run())
    assert created is False
    assert total == 1


def test_wecom_reply_without_pending_creates_new_ticket(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            await _seed(tenant, DATABASE_URL)
            worker = InboundWorker(runtime, batch_size=10, tenant_id=tenant)
            # 无待补全记录的用户直接发消息 -> 新建工单
            event = f"evt-{uuid4().hex}"
            await runtime.tickets.register_inbound_event(
                tenant, "wecom", event, _payload("打印机卡纸了", requester="other-user")
            )
            await worker.run_once()
            record = await runtime.tickets.get_inbound_event(tenant, "wecom", event)
            ticket = await runtime.tickets.get(tenant, record["ticket_id"])
            return record["status"], ticket.title

    status, title = asyncio.run(run())
    assert status == "committed"
    assert title == "打印机卡纸了"


def test_wecom_reply_expired_pending_not_resumed(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            await _seed(tenant, DATABASE_URL)
            worker = InboundWorker(runtime, batch_size=10, tenant_id=tenant)
            first_event = f"evt-{uuid4().hex}"
            await runtime.tickets.register_inbound_event(
                tenant, "wecom", first_event, _payload("VPN 无法连接，错误码 809")
            )
            await worker.run_once()
            ticket = await runtime.tickets.get(
                tenant,
                (await runtime.tickets.get_inbound_event(tenant, "wecom", first_event))[
                    "ticket_id"
                ],
            )
            # 过期待补全
            await runtime.tickets.expire_pending_intakes(now=datetime.now(UTC) + timedelta(days=30))
            # 客户过期后回复 -> 不 resume，按新消息建单
            reply_event = f"evt-{uuid4().hex}"
            await runtime.tickets.register_inbound_event(
                tenant, "wecom", reply_event, _payload(FULL_REPLY)
            )
            await worker.run_once()
            reply_record = await runtime.tickets.get_inbound_event(tenant, "wecom", reply_event)
            total = await _count_tickets(runtime, tenant)
            return reply_record["status"], total, reply_record["ticket_id"] == ticket.ticket_id

    status, total, same_ticket = asyncio.run(run())
    assert status == "committed"
    assert total == 2  # 新消息建了新工单
    assert same_ticket is False
