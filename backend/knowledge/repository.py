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
from .tokenizer import tokenize_for_search


class KnowledgeRepository:
    def __init__(self, pool: AsyncConnectionPool, *, trigram_threshold: float = 0.1) -> None:
        self.pool = pool
        if not 0.0 <= trigram_threshold <= 1.0:
            raise ValueError("trigram_threshold 必须在 0 到 1 之间")
        self.trigram_threshold = trigram_threshold

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
                    tokenized = tokenize_for_search(chunk.content)
                    await cursor.execute(
                        """
                        INSERT INTO knowledge_chunks (
                            tenant_id, document_id, document_version, chunk_id,
                            ordinal, content, search_text, search_vector,
                            embedding_ref, embedding_model, metadata
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, to_tsvector('simple', %s), %s, %s, %s)
                        """,
                        (
                            tenant_id,
                            document.document_id,
                            document.version,
                            chunk.chunk_id,
                            chunk.ordinal,
                            chunk.content,
                            tokenized,
                            tokenized,
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

    async def retire_document(self, tenant_id: str, document_id: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE knowledge_documents SET status = 'retired', updated_at = now()
                    WHERE tenant_id = %s AND document_id = %s AND status = 'published'
                    """,
                    (tenant_id, document_id),
                )
                return cursor.rowcount > 0

    async def list_documents(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT document_id, version, title, source_uri, status, category,
                           visibility, allowed_departments, created_by, valid_from,
                           valid_until, updated_at
                    FROM knowledge_documents
                    WHERE tenant_id = %s
                    ORDER BY updated_at DESC, document_id
                    LIMIT %s OFFSET %s
                    """,
                    (tenant_id, limit, offset),
                )
                return list(await cursor.fetchall())

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
        # 查询与入库使用同一分词器：tokenize_for_search(query) 与 search_text 对齐。
        tokenized = tokenize_for_search(query)
        tsquery_text = tokenized or query
        # OR 路：分词 token 用 | 连接，命中任一 token 即召回，ts_rank 按覆盖率排序，
        # 解决「排查 vs 排查顺序」「错误 vs 错误码」等分词不一致导致 AND 全空的问题。
        or_tsquery = " | ".join(tsquery_text.split())
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            c.tenant_id, c.document_id, c.document_version,
                            c.chunk_id, d.title, c.content, d.source_uri,
                            (
                                SELECT count(*)
                                FROM unnest(string_to_array(lower(%s), ' ')) AS q
                                WHERE position(q IN lower(c.content)) > 0
                            ) AS token_hits,
                            ts_rank_cd(c.search_vector, to_tsquery('simple', %s)) AS or_rank,
                            similarity(c.search_text, %s) AS trgm_rank
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
                          AND (
                              c.search_vector @@ plainto_tsquery('simple', %s)
                              OR c.search_vector @@ to_tsquery('simple', %s)
                              OR similarity(c.search_text, %s) > %s
                          )
                    )
                    SELECT tenant_id, document_id, document_version, chunk_id,
                           title, content, source_uri,
                           row_number() OVER (
                               ORDER BY token_hits DESC, or_rank DESC, trgm_rank DESC,
                                        document_id, chunk_id
                           ) AS source_rank
                    FROM ranked
                    ORDER BY token_hits DESC, or_rank DESC, trgm_rank DESC,
                             document_id, chunk_id
                    LIMIT %s
                    """,
                    (
                        tokenized,
                        or_tsquery,
                        tokenized,
                        principal.tenant_id,
                        principal.internal,
                        principal.internal,
                        list(principal.departments),
                        list(principal.departments),
                        tsquery_text,
                        or_tsquery,
                        tokenized,
                        self.trigram_threshold,
                        limit,
                    ),
                )
                rows = await cursor.fetchall()
        return [RetrievalHit(source="lexical", fused_score=0.0, **row) for row in rows]
