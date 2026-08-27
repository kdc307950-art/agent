import asyncio
import os
from uuid import uuid4

import pytest

from backend.knowledge import (
    KnowledgeChunkInput,
    KnowledgeDocumentInput,
    KnowledgeRepository,
    PgVectorRetriever,
    RetrievalPrincipal,
)
from backend.vector_migrations import setup_vector_schema
from backend.migrations import setup_postgres
from backend.tickets import TicketRepository


DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_knowledge_search_enforces_tenant_status_and_department_acl(monkeypatch):
    tenant_id = f"tenant-{uuid4().hex}"
    other_tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        ticket_repository = await TicketRepository.connect(DATABASE_URL)
        knowledge = KnowledgeRepository(ticket_repository.pool)
        try:
            await knowledge.put_document(
                tenant_id,
                KnowledgeDocumentInput(
                    document_id="public-sso",
                    version=1,
                    title="Public SSO",
                    status="published",
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="reset sso password")],
            )
            await knowledge.put_document(
                tenant_id,
                KnowledgeDocumentInput(
                    document_id="finance-only",
                    version=1,
                    title="Finance SSO",
                    status="published",
                    allowed_departments=("finance",),
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="reset sso finance token")],
            )
            await knowledge.put_document(
                tenant_id,
                KnowledgeDocumentInput(
                    document_id="draft",
                    version=1,
                    title="Draft SSO",
                    status="draft",
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="reset sso draft secret")],
            )
            await knowledge.put_document(
                other_tenant,
                KnowledgeDocumentInput(
                    document_id="other-tenant",
                    version=1,
                    title="Other tenant SSO",
                    status="published",
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="reset sso other tenant")],
            )

            public = await knowledge.lexical_search(
                RetrievalPrincipal(tenant_id=tenant_id),
                "reset sso",
            )
            finance = await knowledge.lexical_search(
                RetrievalPrincipal(tenant_id=tenant_id, departments={"finance"}),
                "reset sso",
            )
            other = await knowledge.lexical_search(
                RetrievalPrincipal(tenant_id=other_tenant),
                "reset sso",
            )
            return public, finance, other
        finally:
            await ticket_repository.close()

    public, finance, other = asyncio.run(run())
    assert {item.document_id for item in public} == {"public-sso"}
    assert {item.document_id for item in finance} == {"public-sso", "finance-only"}
    assert {item.document_id for item in other} == {"other-tenant"}
