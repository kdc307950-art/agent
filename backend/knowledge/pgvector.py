"""pgvector retriever with mandatory tenant and department ACL filters."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx
from psycopg.rows import dict_row

from .models import RetrievalHit, RetrievalPrincipal
from .repository import KnowledgeRepository


class EmbeddingProvider(Protocol):
    async def embed_query(self, text: str) -> Sequence[float]: ...


class HttpEmbeddingProvider:
    def __init__(self, endpoint: str, *, dimension: int, timeout_seconds: float = 15.0) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("Embedding endpoint 必须是 http(s) URL")
        self.endpoint = endpoint
        self.dimension = dimension
        self.timeout = timeout_seconds

    async def embed_query(self, text: str) -> Sequence[float]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.endpoint,
                json={"texts": [text]},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
        payload = response.json()
        embeddings = payload.get("embeddings") or payload.get("data") or []
        vector = embeddings[0] if embeddings else []
        if len(vector) != self.dimension:
            raise ValueError("embedding 维度与配置不一致")
        return [float(value) for value in vector]


class PgVectorRetriever:
    def __init__(
        self,
        repository: KnowledgeRepository,
        embedder: EmbeddingProvider,
        *,
        dimension: int,
    ) -> None:
        if dimension < 8 or dimension > 4096:
            raise ValueError("向量维度必须在 8 到 4096 之间")
        self.repository = repository
        self.embedder = embedder
        self.dimension = dimension

    def _literal(self, embedding: Sequence[float]) -> str:
        if len(embedding) != self.dimension:
            raise ValueError("embedding 维度不匹配")
        values = [float(value) for value in embedding]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("embedding 包含非有限数")
        return "[" + ",".join(format(value, ".9g") for value in values) + "]"

    async def put_embedding(
        self,
        principal: RetrievalPrincipal,
        *,
        document_id: str,
        document_version: int,
        chunk_id: str,
        embedding: Sequence[float],
        embedding_model: str,
    ) -> bool:
        literal = self._literal(embedding)
        async with self.repository.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE knowledge_chunks
                    SET embedding = %s::vector, embedding_model = %s
                    WHERE tenant_id = %s AND document_id = %s
                      AND document_version = %s AND chunk_id = %s
                    """,
                    (literal, embedding_model, principal.tenant_id, document_id, document_version, chunk_id),
                )
                return cursor.rowcount == 1

    async def search(
        self,
        principal: RetrievalPrincipal,
        query: str,
        *,
        limit: int,
    ) -> list[RetrievalHit]:
        if not query.strip():
            return []
        return await self.search_embedding(
            principal, await self.embedder.embed_query(query), limit=limit
        )

    async def search_embedding(
        self,
        principal: RetrievalPrincipal,
        embedding: Sequence[float],
        *,
        limit: int,
    ) -> list[RetrievalHit]:
        if limit < 1 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        literal = self._literal(embedding)
        async with self.repository.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT c.tenant_id, c.document_id, c.document_version, c.chunk_id,
                           d.title, c.content, d.source_uri,
                           row_number() OVER (
                               ORDER BY c.embedding <=> %s::vector, c.document_id, c.chunk_id
                           ) AS source_rank
                    FROM knowledge_chunks AS c
                    JOIN knowledge_documents AS d
                      ON d.tenant_id = c.tenant_id
                     AND d.document_id = c.document_id
                     AND d.version = c.document_version
                    WHERE c.tenant_id = %s AND c.embedding IS NOT NULL
                      AND d.status = 'published'
                      AND (d.valid_from IS NULL OR d.valid_from <= now())
                      AND (d.valid_until IS NULL OR d.valid_until > now())
                      AND (cardinality(d.allowed_departments) = 0
                           OR d.allowed_departments && %s::TEXT[])
                    ORDER BY c.embedding <=> %s::vector, c.document_id, c.chunk_id
                    LIMIT %s
                    """,
                    (literal, principal.tenant_id, list(principal.departments), literal, limit),
                )
                rows = await cursor.fetchall()
        return [RetrievalHit(source="vector", fused_score=0.0, **row) for row in rows]
