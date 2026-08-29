"""租户与部门隔离的 PostgreSQL 知识仓库（数据访问层）。

职责：
    - 文档 / 分块的持久化（UPSERT、分块重建）
    - 版本状态机：draft -> published -> retired 的迁移
    - 词法检索（tsvector + pg_trgm 三路召回）与文档分页列表

关键设计：
    - 所有写操作在单个事务内完成：文档 upsert 与分块重建要么全成、要么全败
    - 所有查询强制 tenant_id 过滤 + 可见性/部门 ACL（与 pgvector 侧保持一致）
    - 查询侧与入库侧共用同一分词器（tokenizer），保证分词一致、可检索
"""

from __future__ import annotations

from typing import Any

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
    """知识仓库：封装全部知识库 SQL，对外暴露高层领域操作。"""

    def __init__(self, pool: AsyncConnectionPool, *, trigram_threshold: float = 0.1) -> None:
        """构造仓库。

        参数：
            pool: psycopg 异步连接池（由运行环境提供）
            trigram_threshold: pg_trgm similarity 的召回阈值（0..1），
                               只有相似度超过它的分块才进入候选集
        """
        self.pool = pool
        # 阈值越界会导致相似度过滤条件恒真 / 恒假，必须提前拦截
        if not 0.0 <= trigram_threshold <= 1.0:
            raise ValueError("trigram_threshold 必须在 0 到 1 之间")
        self.trigram_threshold = trigram_threshold

    async def put_document(
        self,
        tenant_id: str,
        document: KnowledgeDocumentInput,
        chunks: list[KnowledgeChunkInput],
    ) -> None:
        """写入（或幂等覆盖）文档及其全部分块，单事务原子完成。

        参数：
            tenant_id: 租户 ID（写隔离边界）
            document: 文档元信息
            chunks: 该版本的全部分块（至少 1 个）
        设计：
            - UPSERT（ON CONFLICT DO UPDATE）：同一 (tenant, document, version)
              重复写入是覆盖而非报错，天然支持幂等重试
            - 先删后插分块：内容更新会完整重建分块集，不残留旧版本孤立分块
        """
        if not chunks:
            raise ValueError("知识文档必须至少包含一个分块")
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                # 文档行 upsert：同版本已存在则就地更新全部业务字段
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
                # 删除该版本旧分块，随后重建——与上面的 upsert 同处一个事务
                await cursor.execute(
                    """
                    DELETE FROM knowledge_chunks
                    WHERE tenant_id = %s AND document_id = %s AND document_version = %s
                    """,
                    (tenant_id, document.document_id, document.version),
                )
                for chunk in chunks:
                    # 入库侧分词与查询侧共用 tokenize_for_search，保证可检索对齐
                    tokenized = tokenize_for_search(chunk.content)
                    # to_tsvector('simple') 生成词法向量；search_text 供 pg_trgm 相似度
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
        """发布指定版本：同文档其他已发布版本自动停用（单事务）。

        参数：
            tenant_id: 租户 ID
            document_id: 文档 ID
            version: 要发布的版本号
        异常：目标版本不是 draft 时抛 ValueError
        设计：两步 UPDATE 在同一事务内完成——先停用旧的再激活新的，
              任何一步失败整体回滚，保证"同一时间至多一个 published 版本"。
        """
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                # 第一步：把同文档其他 published 版本全部置为 retired
                await cursor.execute(
                    """
                    UPDATE knowledge_documents SET status = 'retired', updated_at = now()
                    WHERE tenant_id = %s AND document_id = %s
                      AND version <> %s AND status = 'published'
                    """,
                    (tenant_id, document_id, version),
                )
                # 第二步：目标版本只有处于 draft 才能转 published
                await cursor.execute(
                    """
                    UPDATE knowledge_documents SET status = 'published', updated_at = now()
                    WHERE tenant_id = %s AND document_id = %s AND version = %s
                      AND status = 'draft'
                    """,
                    (tenant_id, document_id, version),
                )
                # 恰好影响 1 行才是合法迁移；否则说明版本状态不满足前置条件
                if cursor.rowcount != 1:
                    raise ValueError("只能发布 draft 知识文档版本")

    async def retire_document(self, tenant_id: str, document_id: str) -> bool:
        """停用当前已发布版本（若有）。

        参数：tenant_id / document_id: 定位文档
        返回：是否确实停用了某个 published 版本（False 表示本来就没有）
        """
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                # 仅 published -> retired；draft 文档不受影响
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
        """分页列出租户下的文档摘要（按更新时间倒序）。

        参数：
            tenant_id: 租户 ID
            limit: 1..200 条；offset: 分页偏移
        返回：文档行字典列表（不含分块内容，避免大字段拖慢列表页）
        """
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
        """词法检索：tsvector 全文检索 + pg_trgm 模糊匹配三路召回。

        参数：
            principal: 检索主体（tenant + visibility + departments ACL）
            query: 查询文本；空白查询短路返回空列表
            limit: 1..100 条
        返回：按 (token 命中数, ts_rank, trgm 相似度) 降序的命中列表
        设计：三路 OR 召回（plainto_tsquery 精确短语、to_tsquery OR 扩展、
              similarity 模糊兜底），再统一排序，兼顾精确与容错。
        """
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
                # SQL 过滤链与向量检索一致：租户 -> 已发布 + 有效期 -> 可见性分级 -> 部门白名单；
                # token_hits 统计查询词在正文中的命中数，作为第一排序键
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
        # 构造不可变的检索命中；fused_score 由融合阶段（RRF）再填充
        return [RetrievalHit(source="lexical", fused_score=0.0, **row) for row in rows]
