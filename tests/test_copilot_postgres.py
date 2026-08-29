"""Resolution Copilot Postgres 集成测试 —— 持久化 / 幂等 / 审批状态机。

覆盖 PRD 第十节：
- 相同 operation_id 不重复生成（幂等）
- 草稿审批前不会创建客户消息（审批只做状态迁移）
- 模型失败不改变工单状态
- 同一工单可审计多次生成
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from backend.copilot.repository import CopilotRepository
from backend.migrations import setup_postgres
from backend.tickets import CreateTicket, TicketRepository
from src.my_agent.helpdesk import ActorType

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def _seed_ticket_async(tickets: TicketRepository, tenant: str, ticket_id: str):
    return tickets.create(
        tenant,
        CreateTicket(
            ticket_id=ticket_id,
            requester_id="customer-1",
            channel="web",
            title="VPN 无法连接",
            description="客户端无法连接公司 VPN",
            actor_type=ActorType.CUSTOMER,
            actor_id="customer-1",
        ),
    )


def test_copilot_run_and_draft_persistence(monkeypatch):
    """run + draft 落库，latest 查询与审批状态机正确。"""
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:8]}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        repo = CopilotRepository(tickets.pool)
        try:
            await _seed_ticket_async(tickets, tenant, ticket_id)
            run_id = uuid4().hex
            draft_id = uuid4().hex
            created = await repo.start_run(
                run_id=run_id,
                tenant_id=tenant,
                ticket_id=ticket_id,
                operation_id=f"op-{uuid4().hex}",
            )
            assert created is True
            await repo.finish_run(
                run_id=run_id,
                tenant_id=tenant,
                status="completed",
                tool_calls=3,
                latency_ms=120,
            )
            await repo.save_draft(
                draft_id=draft_id,
                tenant_id=tenant,
                ticket_id=ticket_id,
                run_id=run_id,
                draft_answer="请先检查网络连接",
                steps=["检查网络", "重新导入 VPN 配置"],
                citations=[
                    {"document_id": "vpn-guide", "document_version": 2, "chunk_id": "vpn-03"}
                ],
                confidence=0.91,
                needs_human_review=False,
            )
            latest = await repo.get_latest_draft(tenant, ticket_id)
            assert latest is not None
            assert latest["draft_answer"] == "请先检查网络连接"
            assert latest["status"] == "generated"
            assert latest["confidence"] == 0.91

            # 审批：generated -> approved
            approved = await repo.approve_draft(
                tenant_id=tenant, draft_id=draft_id, approved_by="agent-1"
            )
            assert approved is True
            latest2 = await repo.get_latest_draft(tenant, ticket_id)
            assert latest2["status"] == "approved"
            assert latest2["approved_by"] == "agent-1"

            # 已审批的草稿不能再审批（幂等拒绝）
            re_approve = await repo.approve_draft(
                tenant_id=tenant, draft_id=draft_id, approved_by="agent-2"
            )
            assert re_approve is False
        finally:
            await tickets.close()

    asyncio.run(run())


def test_copilot_operation_id_idempotency(monkeypatch):
    """相同 operation_id 不重复生成：第二次 start_run 返回 False，run 记录唯一。"""
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:8]}"
    operation_id = f"op-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        repo = CopilotRepository(tickets.pool)
        try:
            await _seed_ticket_async(tickets, tenant, ticket_id)
            run_id = uuid4().hex
            first = await repo.start_run(
                run_id=run_id,
                tenant_id=tenant,
                ticket_id=ticket_id,
                operation_id=operation_id,
            )
            second = await repo.start_run(
                run_id=uuid4().hex,
                tenant_id=tenant,
                ticket_id=ticket_id,
                operation_id=operation_id,
            )
            assert first is True
            assert second is False  # 幂等：不重复登记

            existing = await repo.get_run_by_operation(tenant, ticket_id, operation_id)
            assert existing is not None
            assert existing["run_id"] == run_id
        finally:
            await tickets.close()

    asyncio.run(run())


def test_copilot_failed_run_keeps_ticket_unchanged(monkeypatch):
    """模型失败只记录 run 失败状态，不改变工单状态/版本。"""
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:8]}"
    operation_id = f"op-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        repo = CopilotRepository(tickets.pool)
        try:
            await _seed_ticket_async(tickets, tenant, ticket_id)
            before = await tickets.get(tenant, ticket_id)
            assert before is not None
            version_before = before.version

            run_id = uuid4().hex
            await repo.start_run(
                run_id=run_id,
                tenant_id=tenant,
                ticket_id=ticket_id,
                operation_id=operation_id,
            )
            await repo.finish_run(
                run_id=run_id,
                tenant_id=tenant,
                status="failed",
                tool_calls=0,
                latency_ms=50,
                error_code="model_failed",
            )

            # 工单状态/版本未被 Copilot 改变
            after = await tickets.get(tenant, ticket_id)
            assert after is not None
            assert after.version == version_before

            # 失败运行记录可审计：按 operation_id 查询
            failed = await repo.get_run_by_operation(tenant, ticket_id, operation_id)
            assert failed is not None
            assert failed["status"] == "failed"
            assert failed["error_code"] == "model_failed"
        finally:
            await tickets.close()

    asyncio.run(run())


def test_copilot_approval_does_not_create_customer_message(monkeypatch):
    """草稿审批只做状态迁移，不创建任何客户消息（无 Outbox 事件）。"""
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:8]}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        repo = CopilotRepository(tickets.pool)
        try:
            await _seed_ticket_async(tickets, tenant, ticket_id)
            run_id = uuid4().hex
            draft_id = uuid4().hex
            await repo.start_run(
                run_id=run_id,
                tenant_id=tenant,
                ticket_id=ticket_id,
                operation_id=f"op-{uuid4().hex}",
            )
            await repo.finish_run(
                run_id=run_id, tenant_id=tenant, status="completed", tool_calls=2, latency_ms=80
            )
            await repo.save_draft(
                draft_id=draft_id,
                tenant_id=tenant,
                ticket_id=ticket_id,
                run_id=run_id,
                draft_answer="草稿",
                steps=[],
                citations=[],
                confidence=0.9,
                needs_human_review=False,
            )
            approved = await repo.approve_draft(
                tenant_id=tenant, draft_id=draft_id, approved_by="agent-1"
            )
            assert approved is True

            # 审批后：copilot_drafts 状态 approved，但 outbox_events 无新事件
            async with tickets.pool.connection() as connection:
                row = await (
                    await connection.execute(
                        "SELECT status FROM copilot_drafts WHERE draft_id = %s", (draft_id,)
                    )
                ).fetchone()
                assert row[0] == "approved"
                outbox = await (
                    await connection.execute(
                        "SELECT count(*) FROM outbox_events WHERE tenant_id = %s AND aggregate_id = %s",
                        (tenant, ticket_id),
                    )
                ).fetchone()
                assert outbox[0] == 0  # 审批不产生客户消息
        finally:
            await tickets.close()

    asyncio.run(run())


def test_copilot_lease_expired_run_is_recovered(monkeypatch):
    """超租约的 running 僵尸运行被 recover 标记 failed，之后可被新 operation 重试。"""
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:8]}"
    operation_id = f"op-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        repo = CopilotRepository(tickets.pool)
        try:
            await _seed_ticket_async(tickets, tenant, ticket_id)
            run_id = uuid4().hex
            created = await repo.start_run(
                run_id=run_id,
                tenant_id=tenant,
                ticket_id=ticket_id,
                operation_id=operation_id,
                lease_seconds=1,  # 极短租约
            )
            assert created is True

            # 把租约改成过去时间（模拟进程崩溃后未续租）
            async with tickets.pool.connection() as connection:
                await connection.execute(
                    "UPDATE copilot_runs SET lease_expires_at = now() - interval '10 seconds'"
                )

            # recover：running 超租约 -> failed(copilot_lease_expired)
            # 全量并发时可能同时恢复其他测试的残留 running，因此只校验 >=1
            recovered = await repo.recover_expired_runs(lease_seconds=1)
            assert recovered >= 1

            existing = await repo.get_run_by_operation(tenant, ticket_id, operation_id)
            assert existing is not None
            assert existing["status"] == "failed"
            assert existing["error_code"] == "copilot_lease_expired"
            # 僵尸运行不再是 running，客户端可用新 operation_id 重试
            assert existing["status"] != "running"
        finally:
            await tickets.close()

    asyncio.run(run())
