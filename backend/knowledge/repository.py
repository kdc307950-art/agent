"""Tenant- and department-scoped PostgreSQL knowledge repository."""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .models import (
    KnowledgeChunkInput,
    KnowledgeDocumentInput,
    RetrievalHit,
    RetrievalPrincipal,
)


class KnowledgeRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def put_document(
        self,
        tenant_id: str,
        document: KnowledgeDocumentInput,
        chunks: list[KnowledgeChunkInput],
    ) -> None:
        if not chunks:
            raise ValueError("知识文档必须至少包含一个分块")
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO knowledge_documents (
                        tenant_id, document_id, version, title, source_uri,
                        status, category, visibility, allowed_departments,
                        created_by, valid_from, valid_until, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, document_id, version) DO UPDATE SET
                        title = EXCLUDED.title,
                        source_uri = EXCLUDED.source_uri,
                        status = EXCLUDED.status,
                        category = EXCLUDED.category,
                        visibility = EXCLUDED.visibility,
                        allowed_departments = EXCLUDED.allowed_departments,
                        created_by = EXCLUDED.created_by,
                        valid_from = EXCLUDED.valid_from,
                        valid_until = EXCLUDED.valid_until,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    """,
                    (
                        tenant_id,
                        document.document_id,
                        document.version,
                        document.title,
                        document.source_uri,
                        document.status,
                        document.category,
                        document.visibility,
                        list(document.allowed_departments),
                        document.created_by,
                        document.valid_from,
                        document.valid_until,
                        Jsonb(document.metadata),
                    ),
                )
                await cursor.execute(
                    """
                    DELETE FROM knowledge_chunks
                    WHERE tenant_id = %s AND document_id = %s AND document_version = %s
                    """,
                    (tenant_id, document.document_id, document.version),
                )
                for chunk in chunks:
                    await cursor.execute(
                        """
                        INSERT INTO knowledge_chunks (
                            tenant_id, document_id, document_version, chunk_id,
                            ordinal, content, embedding_ref, embedding_model, metadata
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            tenant_id,
                            document.document_id,
                            document.version,
                            chunk.chunk_id,
                            chunk.ordinal,
                            chunk.content,
                            chunk.embedding_ref,
                            chunk.embedding_model,
                            Jsonb(chunk.metadata),
                        ),
                    )

    async def publish_document_version(
        self,
        tenant_id: str,
        document_id: str,
        version: int,
    ) -> None:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE knowledge_documents SET status = 'retired', updated_at = now()
                    WHERE tenant_id = %s AND document_id = %s
                      AND version <> %s AND status = 'published'
                    """,
                    (tenant_id, document_id, version),
                )
                await cursor.execute(
                    """
                    UPDATE knowledge_documents SET status = 'published', updated_at = now()
                    WHERE tenant_id = %s AND document_id = %s AND version = %s
                      AND status = 'draft'
                    """,
                    (tenant_id, document_id, version),
                )
                if cursor.rowcount != 1:
                    raise ValueError("只能发布 draft 知识文档版本")

    async def lexical_search(
        self,
        principal: RetrievalPrincipal,
        query: str,
        *,
        limit: int = 10,
    ) -> list[RetrievalHit]:
        if not query.strip():
            return []
        if limit < 1 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            c.tenant_id, c.document_id, c.document_version,
                            c.chunk_id, d.title, c.content, d.source_uri,
                            ts_rank_cd(c.search_vector, websearch_to_tsquery('simple', %s)) AS rank
                        FROM knowledge_chunks AS c
                        JOIN knowledge_documents AS d
                          ON d.tenant_id = c.tenant_id
                         AND d.document_id = c.document_id
                         AND d.version = c.document_version
                        WHERE c.tenant_id = %s
                          AND d.status = 'published'
                          AND (d.valid_from IS NULL OR d.valid_from <= now())
                          AND (d.valid_until IS NULL OR d.valid_until > now())
                          AND (
                              d.visibility = 'public'
                              OR (d.visibility = 'internal' AND %s::boolean)
                              OR (
                                  d.visibility = 'restricted' AND %s::boolean
                                  AND cardinality(d.allowed_departments) > 0
                                  AND d.allowed_departments && %s::TEXT[]
                              )
                          )
                          AND (
                              cardinality(d.allowed_departments) = 0
                              OR d.allowed_departments && %s::TEXT[]
                          )
                          AND c.search_vector @@ websearch_to_tsquery('simple', %s)
                    )
                    SELECT tenant_id, document_id, document_version, chunk_id,
                           title, content, source_uri,
                           row_number() OVER (ORDER BY rank DESC, document_id, chunk_id) AS source_rank
                    FROM ranked
                    ORDER BY rank DESC, document_id, chunk_id
                    LIMIT %s
                    """,
                    (
                        query,
                        principal.tenant_id,
                        principal.internal,
                        principal.internal,
                        list(principal.departments),
                        list(principal.departments),
                        query,
                        limit,
                    ),
                )
                rows = await cursor.fetchall()
        return [RetrievalHit(source="lexical", fused_score=0.0, **row) for row in rows]
