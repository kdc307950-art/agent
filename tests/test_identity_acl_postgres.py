"""部门身份透传与 ACL 集成测试 —— 阶段一权限模型收敛。

覆盖方案验收标准：
- 租户 A 不可检索租户 B
- 普通员工不可见 restricted 文档
- IT 部门可见 IT restricted 文档
- 财务部门不可见 IT restricted 文档
- 文档过期后不能作为 Copilot 引用（verify_citations 拒绝）
- 前端传入伪造部门不改变权限（主体来自服务端 RunContext，不读请求参数）
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.knowledge.identity import retrieval_principal
from backend.knowledge.models import KnowledgeChunkInput, KnowledgeDocumentInput
from backend.knowledge.repository import KnowledgeRepository
from backend.migrations import setup_postgres
from backend.run_context import RunContext

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def _run_context(
    *,
    tenant_id: str,
    user_id: str,
    departments: frozenset[str] = frozenset(),
    internal: bool = False,
) -> RunContext:
    return RunContext(
        run_id="run-acl",
        request_id="req-acl",
        tenant_id=tenant_id,
        user_id=user_id,
        thread_id=f"acl:{tenant_id}:{user_id}",
        scopes=frozenset({"ticket:agent"}),
        deadline=asyncio.get_running_loop().time() + 60,
        role="agent" if internal else "customer",
        departments=departments,
        internal=internal,
    )


async def _seed(repo, tenant: str, doc_id: str, visibility: str, departments=(), status="published"):
    await repo.put_document(
        tenant,
        KnowledgeDocumentInput(
            document_id=doc_id,
            version=1,
            title=f"{doc_id} 标题",
            status=status,  # type: ignore[arg-type]
            visibility=visibility,  # type: ignore[arg-type]
            allowed_departments=tuple(departments),
        ),
        [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content=f"{doc_id} 正文")],
    )


def test_retrieval_principal_derives_identity_from_run_context():
    """统一封装：主体从 RunContext 派生，含租户/部门/internal。"""
    import time

    ctx = RunContext(
        run_id="run-acl",
        request_id="req-acl",
        tenant_id="tenant-a",
        user_id="agent-1",
        thread_id="acl:tenant-a:agent-1",
        scopes=frozenset({"ticket:agent"}),
        deadline=time.monotonic() + 60,
        role="agent",
        departments=frozenset({"it"}),
        internal=True,
    )
    principal = retrieval_principal(ctx)
    assert principal.tenant_id == "tenant-a"
    assert principal.departments == frozenset({"it"})
    assert principal.internal is True


def test_across_tenant_and_department_acl(monkeypatch):
    """跨租户不可见；restricted 文档仅对应部门可见（IT/财务隔离）。"""
    tenant_a = f"tenant-{uuid4().hex}"
    tenant_b = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = KnowledgeRepository(audit.pool)
            await _seed(repo, tenant_a, "it-guide", "restricted", departments=("it",))
            await _seed(repo, tenant_a, "public-guide", "public")
            await _seed(repo, tenant_b, "b-guide", "internal")

            # 租户 A 普通员工（internal=True 无部门）：只见 public
            employee = _run_context(tenant_id=tenant_a, user_id="emp-1", internal=True)
            hits = await repo.lexical_search(retrieval_principal(employee), "guide", limit=20)
            assert {h.document_id for h in hits} == {"public-guide"}

            # IT 部门坐席：可见 public + it restricted
            it_agent = _run_context(
                tenant_id=tenant_a, user_id="it-1", departments=frozenset({"it"}), internal=True
            )
            hits_it = await repo.lexical_search(retrieval_principal(it_agent), "guide", limit=20)
            assert {"it-guide", "public-guide"}.issubset({h.document_id for h in hits_it})

            # 财务部门坐席：不可见 IT restricted
            finance_agent = _run_context(
                tenant_id=tenant_a, user_id="fin-1", departments=frozenset({"finance"}), internal=True
            )
            hits_fin = await repo.lexical_search(retrieval_principal(finance_agent), "guide", limit=20)
            assert "it-guide" not in {h.document_id for h in hits_fin}

            # 租户 B 不可见租户 A 任何文档
            b_agent = _run_context(tenant_id=tenant_b, user_id="b-1", internal=True)
            hits_b = await repo.lexical_search(retrieval_principal(b_agent), "guide", limit=20)
            assert all(h.document_id == "b-guide" for h in hits_b)

    asyncio.run(run())


def test_expired_document_cannot_be_copilot_citation(monkeypatch):
    """文档过期后不能作为 Copilot 引用（verify_citations 权威校验拒绝）。"""
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = KnowledgeRepository(audit.pool)
            past = datetime.now(UTC) - timedelta(days=1)
            await repo.put_document(
                tenant,
                KnowledgeDocumentInput(
                    document_id="expired-doc",
                    version=1,
                    title="过期文档",
                    status="published",
                    visibility="internal",
                    valid_from=None,
                    valid_until=past,  # 已过期
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="过期内容")],
            )
            ctx = _run_context(tenant_id=tenant, user_id="agent-1", internal=True)
            verified = await repo.verify_citations(
                retrieval_principal(ctx), [("expired-doc", 1, "c1")]
            )
            assert verified == []  # 过期文档不能作为引用

    asyncio.run(run())


def test_forged_departments_from_request_cannot_escalate(monkeypatch):
    """前端伪造部门不改变权限：主体必须来自服务端 RunContext。

    即使请求体声称 departments=["it"]，检索仍以服务端上下文为准——
    验证服务端上下文无部门时 restricted 文档不可见。
    """
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = KnowledgeRepository(audit.pool)
            await _seed(repo, tenant, "restricted-doc", "restricted", departments=("it",))

            # 服务端上下文：无部门（伪造的 "it" 未被接受）
            server_ctx = _run_context(tenant_id=tenant, user_id="agent-1", internal=True)
            hits = await repo.lexical_search(retrieval_principal(server_ctx), "restricted", limit=20)
            assert "restricted-doc" not in {h.document_id for h in hits}

            # 对照：服务端确有 it 部门时才可见
            real_it = _run_context(
                tenant_id=tenant, user_id="agent-1", departments=frozenset({"it"}), internal=True
            )
            hits_real = await repo.lexical_search(retrieval_principal(real_it), "restricted", limit=20)
            assert "restricted-doc" in {h.document_id for h in hits_real}

    asyncio.run(run())
