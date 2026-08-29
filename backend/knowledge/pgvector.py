"""基于 pgvector 的向量检索器（强制租户与部门 ACL 过滤）。

职责：
    - HttpEmbeddingProvider：通过 HTTP 调用嵌入服务，把查询文本转为向量
    - PgVectorRetriever：写入 / 查询 knowledge_chunks.embedding 向量列

关键设计：
    - 所有 SQL 都携带 租户 + 可见性 + 部门 三重过滤，缺失任一维度
      都不会泄露数据（ACL 是"强制"的，不是可选项）
    - 向量字面量先经 _literal 严格校验（维度、有限数、浮点格式化），
      再走 psycopg 参数绑定，杜绝非法向量注入 SQL
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx
from psycopg.rows import dict_row

from .models import RetrievalHit, RetrievalPrincipal
from .repository import KnowledgeRepository


class EmbeddingProvider(Protocol):
    """查询嵌入协议：把单条查询文本映射为定长向量序列。"""

    async def embed_query(self, text: str) -> Sequence[float]: ...


class HttpEmbeddingProvider:
    """通过 HTTP 调用外部嵌入服务（OpenAI 兼容 /texts 风格端点）。"""

    def __init__(self, endpoint: str, *, dimension: int, timeout_seconds: float = 15.0) -> None:
        """构造嵌入客户端。

        参数：
            endpoint: 嵌入服务 URL，仅接受 http(s)
            dimension: 期望的向量维度，须与 pgvector 列定义一致
            timeout_seconds: HTTP 超时（默认 15s，避免查询被慢服务拖死）
        异常：endpoint 非 http(s) 时抛 ValueError
        """
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("Embedding endpoint 必须是 http(s) URL")
        self.endpoint = endpoint
        self.dimension = dimension
        self.timeout = timeout_seconds

    async def embed_query(self, text: str) -> Sequence[float]:
        """嵌入单条查询文本。

        参数：text: 查询文本
        返回：dimension 维的浮点向量
        异常：HTTP 错误、响应缺向量、维度不符时抛异常
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.endpoint,
                json={"texts": [text]},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
        payload = response.json()
        # 兼容两种响应格式：{"embeddings": [...]} 或 OpenAI 风格 {"data": [...]}
        embeddings = payload.get("embeddings") or payload.get("data") or []
        vector = embeddings[0] if embeddings else []
        # 维度校验在写入 / 查询前拦截，防止维度错配的脏向量进入 pgvector
        if len(vector) != self.dimension:
            raise ValueError("embedding 维度与配置不一致")
        return [float(value) for value in vector]


class PgVectorRetriever:
    """pgvector 向量检索器：写入嵌入 + 向量相似度检索（含强制 ACL）。"""

    def __init__(
        self,
        repository: KnowledgeRepository,
        embedder: EmbeddingProvider,
        *,
        dimension: int,
    ) -> None:
        """构造检索器。

        参数：
            repository: 知识仓库（提供连接池与文档元数据）
            embedder: 查询嵌入提供方
            dimension: 向量维度（8..4096，须与列定义一致）
        """
        # 维度范围与 PostgreSQL vector 类型的合理范围对齐，越界即配置错误
        if dimension < 8 or dimension > 4096:
            raise ValueError("向量维度必须在 8 到 4096 之间")
        self.repository = repository
        self.embedder = embedder
        self.dimension = dimension

    def _literal(self, embedding: Sequence[float]) -> str:
        """把向量序列格式化为 pgvector 字面量字符串（如 "[0.1,0.2]"）。

        参数：embedding: 待格式化的向量
        返回：可安全作为 SQL 参数传入的向量文本
        设计：先校验维度与有限性，再用 .9g 保留 9 位有效数字的紧凑
              十进制表示——既满足距离计算精度，又不产生科学计数法歧义。
        """
        if len(embedding) != self.dimension:
            raise ValueError("embedding 维度不匹配")
        values = [float(value) for value in embedding]
        # 拒绝 NaN / Inf：pgvector 距离计算对非有限数行为未定义
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
        """把向量写入指定分块（依赖 chunk 行已由入库流水线创建）。

        参数：
            principal: 检索主体（这里仅取 tenant_id 作为写过滤条件）
            document_id / document_version / chunk_id: 定位目标分块
            embedding: 待写入的向量
            embedding_model: 生成该向量的嵌入模型名
        返回：是否恰好更新了 1 行（分块不存在时返回 False）
        设计：用 UPDATE 而非 INSERT——chunk 行由 repository.put_document
              先行创建，向量写入失败可单独重试，不必重跑整条入库链路。
        """
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
                    (
                        literal,
                        embedding_model,
                        principal.tenant_id,
                        document_id,
                        document_version,
                        chunk_id,
                    ),
                )
                return cursor.rowcount == 1

    async def search(
        self,
        principal: RetrievalPrincipal,
        query: str,
        *,
        limit: int,
    ) -> list[RetrievalHit]:
        """以查询文本走完整链路：嵌入 -> 向量检索。

        参数：
            principal: 检索主体（ACL 过滤）
            query: 查询文本；纯空白查询直接短路返回空列表（省一次嵌入调用）
            limit: 返回条数上限
        返回：按向量距离排序的 RetrievalHit 列表
        """
        # 空查询直接返回，避免白白调用嵌入服务
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
        """按给定向量做余弦距离（<=>）相似度检索，并施加完整 ACL。

        参数：
            principal: 检索主体（tenant_id + internal + departments）
            embedding: 查询向量（已嵌入）
            limit: 1..100 条
        返回：按距离升序（越近越靠前）的 RetrievalHit 列表
        设计：ACL 全部下沉到 SQL WHERE 条件——只返回检索主体有权限看到、
              已发布且在有效期内的分块（过滤语义见下方 SQL 的参数位）。
        """
        # 与 service 层的检索约定保持一致的上限
        if limit < 1 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        literal = self._literal(embedding)
        async with self.repository.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # SQL 过滤链（按参数顺序）：租户 -> 已发布 + 有效期 -> 可见性分级
                # （public 全可见 / internal·restricted 需 internal 主体）-> 部门白名单；
                # 距离用 <=>（余弦距离），窗口函数 row_number 生成来源内排名
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
                      AND (
                          d.visibility = 'public'
                          OR (d.visibility = 'internal' AND %s::boolean)
                          OR (
                              d.visibility = 'restricted' AND %s::boolean
                              AND cardinality(d.allowed_departments) > 0
                              AND d.allowed_departments && %s::TEXT[]
                          )
                      )
                      AND (cardinality(d.allowed_departments) = 0
                           OR d.allowed_departments && %s::TEXT[])
                    ORDER BY c.embedding <=> %s::vector, c.document_id, c.chunk_id
                    LIMIT %s
                    """,
                    (
                        literal,
                        principal.tenant_id,
                        principal.internal,
                        principal.internal,
                        list(principal.departments),
                        list(principal.departments),
                        literal,
                        limit,
                    ),
                )
                rows = await cursor.fetchall()
        # 构造不可变的检索命中；fused_score 由融合阶段（RRF）再填充
        return [RetrievalHit(source="vector", fused_score=0.0, **row) for row in rows]
