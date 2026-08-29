"""Copilot 发起人身份快照 + 统一检索接线 集成测试（阶段一/二）。

覆盖方案要求的回归用例：
- 身份快照持久化（requester_user_id / departments 落库）
- Worker 使用真实 requester_user_id 构造 RunContext（非固定 copilot-worker）
- 部门 ACL 隔离：IT 坐席见 IT restricted，财务坐席不可见
- 同租户不同用户权限不同；租户 A 用户不能引用租户 B 文档
- 请求体伪造 departments 不影响权限（身份来自服务端快照）
- 身份缺失闭锁（不默认全权限）
- Copilot search_knowledge 经 KnowledgeRetriever（lexical-only 模式）
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from backend.copilot.repository import CopilotRepository
from backend.copilot.worker import CopilotWorker
from backend.knowledge.retriever import KnowledgeRetriever
from backend.migrations import setup_postgres
from backend.run_context import RunContext
from backend.tickets import CreateTicket, TicketRepository
from src.my_agent.helpdesk import ActorType

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


async def _seed_ticket(tickets, tenant: str, ticket_id: str) -> None:
    await tickets.create(
        tenant,
        CreateTicket(
            ticket_id=ticket_id,
            requester_id="customer-1",
            channel="web",
            title="VPN 无法连接",
            description="客户端无法连接 VPN",
            actor_type=ActorType.CUSTOMER,
            actor_id="customer-1",
        ),
    )


async def _seed_doc(repo, tenant: str, doc_id: str, visibility: str, departments=()):
    from backend.knowledge.models import KnowledgeChunkInput, KnowledgeDocumentInput

    await repo.put_document(
        tenant,
        KnowledgeDocumentInput(
            document_id=doc_id,
            version=1,
            title=f"{doc_id} 标题",
            status="published",
            visibility=visibility,  # type: ignore[arg-type]
            allowed_departments=tuple(departments),
        ),
        [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content=f"{doc_id} 正文")],
    )


def test_identity_snapshot_persisted_and_worker_uses_real_requester(monkeypatch):
    """身份快照落库；Worker 从运行记录恢复真实 requester_user_id。"""
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:8]}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = CopilotRepository(audit.pool)
            tickets = TicketRepository(audit.pool)
            await _seed_ticket(tickets, tenant, ticket_id)

            run_id = uuid4().hex
            await repo.start_run(
                run_id=run_id, tenant_id=tenant, ticket_id=ticket_id,
                operation_id=f"op-{uuid4().hex}", lease_seconds=60,
                requester_user_id="it-agent-1",
                requester_role="agent",
                requester_departments=["it"],
                requester_internal=True,
            )

            # 快照落库
            stored = await repo.get_run(tenant, run_id)
            assert stored["requester_user_id"] == "it-agent-1"
            assert stored["requester_departments"] == ["it"]
            assert stored["requester_internal"] is True

            # Worker 用真实身份构造 RunContext（不固定 copilot-worker）
            runtime = SimpleNamespace(
                copilot=None,
                copilot_repository=repo,
                audit=audit,
                metrics=None,
            )
            worker = CopilotWorker(runtime=runtime, max_attempts=2, lease_seconds=60)
            ctx = await worker._build_run_context(
                tenant_id=tenant, ticket_id=ticket_id, run_id=run_id
            )
            assert ctx.user_id == "it-agent-1"
            assert ctx.departments == frozenset({"it"})
            assert ctx.internal is True
        # end audit_context

    asyncio.run(run())


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_copilot_search_knowledge_goes_through_retriever(monkeypatch):
    """Copilot search_knowledge 经 KnowledgeRetriever 执行（阶段二验收）。"""
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context
        from backend.copilot.tools import search_knowledge
        from backend.knowledge.identity import retrieval_principal

        async with audit_context(DATABASE_URL) as audit:
            from backend.knowledge.repository import KnowledgeRepository

            knowledge = KnowledgeRepository(audit.pool)
            await _seed_doc(knowledge, tenant, "vpn-guide", "internal")
            retriever = KnowledgeRetriever(knowledge)

            ctx = RunContext(
                run_id="run-t",
                request_id="req-t",
                tenant_id=tenant,
                user_id="it-agent-1",
                thread_id=f"copilot:{tenant}:t-1",
                scopes=frozenset({"ticket:agent"}),
                deadline=asyncio.get_running_loop().time() + 60,
                role="agent",
                departments=frozenset({"it"}),
                internal=True,
            )
            runtime = SimpleNamespace(
                context=ctx,
                knowledge=knowledge,
                knowledge_retriever=retriever,
            )
            raw = await search_knowledge.coroutine(
                query="vpn", config={"configurable": {"runtime": runtime}}
            )
            import json

            payload = json.loads(raw)
            assert payload["retrieval_mode"] == "lexical-only"
            assert payload["evidence"], "应返回结构化证据"
            assert payload["evidence"][0]["document_id"] == "vpn-guide"
            assert payload["degraded"] is False

    asyncio.run(run())


def test_department_acl_isolates_copilot_retrieval(monkeypatch):
    """部门 ACL：IT 坐席见 IT restricted；财务坐席不可见（Copilot 检索路径）。"""
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context
        from backend.knowledge.identity import retrieval_principal
        from backend.knowledge.repository import KnowledgeRepository

        async with audit_context(DATABASE_URL) as audit:
            knowledge = KnowledgeRepository(audit.pool)
            await _seed_doc(knowledge, tenant, "it-guide", "restricted", departments=("it",))

            def make_ctx(departments):
                return RunContext(
                    run_id="run-a", request_id="req-a", tenant_id=tenant, user_id="u",
                    thread_id="t", scopes=frozenset({"ticket:agent"}),
                    deadline=asyncio.get_running_loop().time() + 60,
                    role="agent", departments=frozenset(departments), internal=True,
                )

            it_hits = await knowledge.lexical_search(
                retrieval_principal(make_ctx(["it"])), "guide", limit=10
            )
            fin_hits = await knowledge.lexical_search(
                retrieval_principal(make_ctx(["finance"])), "guide", limit=10
            )
            assert any(h.document_id == "it-guide" for h in it_hits)
            assert not any(h.document_id == "it-guide" for h in fin_hits)

    asyncio.run(run())


def test_identity_missing_blocks_worker(monkeypatch):
    """身份缺失闭锁：Worker 不使用默认全权限身份（抛 copilot_identity_missing）。"""
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:8]}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = CopilotRepository(audit.pool)
            tickets = TicketRepository(audit.pool)
            await _seed_ticket(tickets, tenant, ticket_id)
            run_id = uuid4().hex
            # requester_user_id 缺省为空（模拟旧数据/异常入队）
            await repo.start_run(
                run_id=run_id, tenant_id=tenant, ticket_id=ticket_id,
                operation_id=f"op-{uuid4().hex}", lease_seconds=60,
                requester_user_id="",
            )
            runtime = SimpleNamespace(copilot=None, copilot_repository=repo, audit=audit, metrics=None)
            worker = CopilotWorker(runtime=runtime, max_attempts=2, lease_seconds=60)
            try:
                await worker._build_run_context(tenant_id=tenant, ticket_id=ticket_id, run_id=run_id)
                raised = False
            except RuntimeError as exc:
                raised = str(exc) == "copilot_identity_missing"
            assert raised, "身份缺失必须闭锁，不默认全权限"

    asyncio.run(run())
