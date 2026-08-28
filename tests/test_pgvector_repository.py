import asyncio
import os
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from backend.knowledge import (
    KnowledgeChunkInput,
    KnowledgeDocumentInput,
    KnowledgeRepository,
    PgVectorRetriever,
    RetrievalPrincipal,
)
from backend.tickets import TicketRepository
from backend.vector_migrations import setup_vector_schema


DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")

# 与评测/导入流水线对齐：vector_migrations 建 1536 维列，测试不再假设列不存在或维度为 8。
_EMBEDDING_DIMENSION = 1536
_EMBEDDING = [1.0] + [0.0] * (_EMBEDDING_DIMENSION - 1)


class Embedder:
    async def embed_query(self, text):
        return _EMBEDDING


def test_pgvector_search_enforces_tenant_and_department_acl(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    other = f"tenant-{uuid4().hex}"

    async def run():
        async with await AsyncConnection.connect(DATABASE_URL) as connection:
            row = await (await connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
            )).fetchone()
        if not row[0]:
            return None
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_vector_schema(dimension=_EMBEDDING_DIMENSION)
        tickets = await TicketRepository.connect(DATABASE_URL)
        repository = KnowledgeRepository(tickets.pool)
        retriever = PgVectorRetriever(repository, Embedder(), dimension=_EMBEDDING_DIMENSION)
        try:
            for tenant_id, document_id, departments, visibility in (
                (tenant, "public", (), "public"),
                (tenant, "finance", ("finance",), "restricted"),
                (other, "other", (), "public"),
            ):
                await repository.put_document(
                    tenant_id,
                    KnowledgeDocumentInput(
                        document_id=document_id,
                        version=1,
                        title=document_id,
                        status="published",
                        visibility=visibility,
                        allowed_departments=departments,
                    ),
                    [KnowledgeChunkInput(chunk_id="c1", ordinal=0, content=document_id)],
                )
                await retriever.put_embedding(
                    RetrievalPrincipal(tenant_id=tenant_id, departments=set(departments)),
                    document_id=document_id,
                    document_version=1,
                    chunk_id="c1",
                    embedding=_EMBEDDING,
                    embedding_model="test-1536",
                )
            public = await retriever.search(
                RetrievalPrincipal(tenant_id=tenant), "query", limit=10
            )
            finance = await retriever.search(
                RetrievalPrincipal(tenant_id=tenant, departments={"finance"}, internal=True),
                "query",
                limit=10,
            )
            return public, finance
        finally:
            await tickets.close()

    result = asyncio.run(run())
    if result is None:
        pytest.skip("pgvector extension is not available in this PostgreSQL")
    public, finance = result
    assert {hit.document_id for hit in public} == {"public"}
    assert {hit.document_id for hit in finance} == {"public", "finance"}
    assert all(hit.tenant_id == tenant for hit in [*public, *finance])
