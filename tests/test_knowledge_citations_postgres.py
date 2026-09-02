"""知识证据权威校验（verify_citations）集成测试 —— 阶段二第二层门禁。

覆盖方案测试场景（引用保存前再次确认）：
- 合法 published 引用通过
- 未发布（draft/retired）文档拒绝
- 跨租户引用拒绝
- chunk 不存在拒绝
- 版本不一致拒绝
- 部门 ACL 拒绝/放行
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.knowledge.models import (
    KnowledgeChunkInput,
    KnowledgeDocumentInput,
    KnowledgeEvidence,
    RetrievalPrincipal,
)
from backend.knowledge.repository import KnowledgeRepository
from backend.migrations import setup_postgres

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


async def _seed_doc(repo, tenant: str, document_id: str, status: str) -> None:
    """插入文档 + 一个 chunk（published 状态时可直接检索/校验）。"""
    await repo.put_document(
        tenant,
        KnowledgeDocumentInput(
            document_id=document_id,
            version=1,
            title=f"{document_id} 标题",
            status=status,  # type: ignore[arg-type]
            visibility="internal",
        ),
        [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content=f"{document_id} 正文内容")],
    )


def test_verify_citations_accepts_published_and_rejects_invalid(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    other_tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = KnowledgeRepository(audit.pool)
            await _seed_doc(repo, tenant, "vpn-guide", "published")
            await _seed_doc(repo, tenant, "draft-doc", "draft")
            await _seed_doc(repo, tenant, "retired-doc", "retired")
            await _seed_doc(repo, other_tenant, "cross-tenant-doc", "published")

            principal = RetrievalPrincipal(tenant_id=tenant, departments=frozenset(), internal=True)

            # 合法引用：published + chunk 存在
            ok = await repo.verify_citations(principal, [("vpn-guide", 1, "c1")])
            assert [e.citation_key for e in ok] == [("vpn-guide", 1, "c1")]
            assert isinstance(ok[0], KnowledgeEvidence)

            # 未发布（draft）：拒绝
            assert await repo.verify_citations(principal, [("draft-doc", 1, "c1")]) == []

            # 已废弃（retired）：拒绝
            assert await repo.verify_citations(principal, [("retired-doc", 1, "c1")]) == []

            # 跨租户：拒绝
            assert await repo.verify_citations(principal, [("cross-tenant-doc", 1, "c1")]) == []

            # chunk 不存在：拒绝
            assert await repo.verify_citations(principal, [("vpn-guide", 1, "no-such-chunk")]) == []

            # 版本不一致：拒绝
            assert await repo.verify_citations(principal, [("vpn-guide", 99, "c1")]) == []

    asyncio.run(run())


def test_verify_citations_respects_department_acl(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = KnowledgeRepository(audit.pool)
            # restricted 文档仅 it 部门可见
            await repo.put_document(
                tenant,
                KnowledgeDocumentInput(
                    document_id="restricted-doc",
                    version=1,
                    title="受限文档",
                    status="published",
                    visibility="restricted",
                    allowed_departments=("it",),
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="受限内容")],
            )

            # 无部门（internal=True 但无 it）：ACL 拒绝
            no_dept = RetrievalPrincipal(tenant_id=tenant, departments=frozenset(), internal=True)
            assert await repo.verify_citations(no_dept, [("restricted-doc", 1, "c1")]) == []

            # it 部门：通过
            it_dept = RetrievalPrincipal(tenant_id=tenant, departments=frozenset({"it"}), internal=True)
            allowed = await repo.verify_citations(it_dept, [("restricted-doc", 1, "c1")])
            assert [e.citation_key for e in allowed] == [("restricted-doc", 1, "c1")]

    asyncio.run(run())


def test_verify_citations_rejects_empty_expired_and_wrong_department(monkeypatch):
    """Day 5：无引用、过期文档、跨部门文档一律不得作为证据。"""
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = KnowledgeRepository(audit.pool)
            await repo.put_document(
                tenant,
                KnowledgeDocumentInput(
                    document_id="expired-doc",
                    version=1,
                    title="已过期文档",
                    status="published",
                    visibility="internal",
                    valid_until=datetime.now(UTC) - timedelta(days=1),
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="过期内容")],
            )
            await repo.put_document(
                tenant,
                KnowledgeDocumentInput(
                    document_id="finance-only-doc",
                    version=1,
                    title="财务部门文档",
                    status="published",
                    visibility="restricted",
                    allowed_departments=("finance",),
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="财务内容")],
            )
            it_principal = RetrievalPrincipal(
                tenant_id=tenant, departments=frozenset({"it"}), internal=True
            )

            # 无引用：空列表直接拒绝
            assert await repo.verify_citations(it_principal, []) == []

            # 过期文档：有效期窗口之外拒绝
            assert await repo.verify_citations(it_principal, [("expired-doc", 1, "c1")]) == []

            # 跨部门文档：it 部门不可引用 finance-only 文档
            assert (
                await repo.verify_citations(it_principal, [("finance-only-doc", 1, "c1")]) == []
            )

    asyncio.run(run())
