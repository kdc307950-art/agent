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


def test_knowledge_visibility_public_internal_restricted_matrix(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        repository = KnowledgeRepository(tickets.pool)
        try:
            docs = {
                "pub": KnowledgeDocumentInput(
                    document_id="pub", version=1, title="Public", status="published", visibility="public"
                ),
                "int": KnowledgeDocumentInput(
                    document_id="int", version=1, title="Internal", status="published", visibility="internal"
                ),
                "res-it": KnowledgeDocumentInput(
                    document_id="res-it", version=1, title="Restricted IT", status="published",
                    visibility="restricted", allowed_departments=("it",),
                ),
                "res-fin": KnowledgeDocumentInput(
                    document_id="res-fin", version=1, title="Restricted Finance", status="published",
                    visibility="restricted", allowed_departments=("finance",),
                ),
            }
            for doc in docs.values():
                await repository.put_document(
                    tenant, doc, [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="Public Internal Restricted")]
                )

            customer = await repository.lexical_search(
                RetrievalPrincipal(tenant_id=tenant), "Public Internal Restricted", limit=10
            )
            agent_no_dept = await repository.lexical_search(
                RetrievalPrincipal(tenant_id=tenant, internal=True), "Public Internal Restricted", limit=10
            )
            agent_it = await repository.lexical_search(
                RetrievalPrincipal(tenant_id=tenant, departments={"it"}, internal=True),
                "Public Internal Restricted",
                limit=10,
            )
            return (
                {hit.document_id for hit in customer},
                {hit.document_id for hit in agent_no_dept},
                {hit.document_id for hit in agent_it},
            )
        finally:
            await tickets.close()

    customer, agent_no_dept, agent_it = asyncio.run(run())
    assert customer == {"pub"}
    assert agent_no_dept == {"pub", "int"}
    assert agent_it == {"pub", "int", "res-it"}


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
                    visibility="public",
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
                    visibility="restricted",
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
                    visibility="public",
                ),
                [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content="reset sso other tenant")],
            )

            public = await knowledge.lexical_search(
                RetrievalPrincipal(tenant_id=tenant_id),
                "reset sso",
            )
            finance = await knowledge.lexical_search(
                RetrievalPrincipal(tenant_id=tenant_id, departments={"finance"}, internal=True),
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
