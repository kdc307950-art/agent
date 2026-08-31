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
    """通过 HTTP 调用外部嵌入服务（OpenAI 兼容 /texts 风格端点）。

    阶段三约束：
        - 文档嵌入走批量 texts 请求，单批最多 EMBED_BATCH_SIZE（32）条
        - 复用同一个 httpx.AsyncClient（连接复用，避免每批新建握手）
        - 任一向量异常（HTTP 错误/维度不符/有限值异常）直接失败，不静默丢弃
    """

    # 单批最多嵌入条数（阶段三：批量约束 ≤32）
    EMBED_BATCH_SIZE = 32

    def __init__(
        self,
        endpoint: str,
        *,
        dimension: int,
        auth_token: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        """构造嵌入客户端。

        参数：
            endpoint: 嵌入服务 URL，仅接受 http(s)
            dimension: 期望的向量维度，须与 pgvector 列定义一致
            timeout_seconds: HTTP 超时（默认 15s，避免查询被慢服务拖死）
        异常：endpoint 非 http(s) 时抛 ValueError
        """
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("Embedding endpoint 必须是 http(s) URL")
        if dimension < 1 or timeout_seconds <= 0:
            raise ValueError("Embedding dimension/timeout 配置无效")
        self.endpoint = endpoint
        self.dimension = dimension
        self.auth_token = auth_token.strip() if auth_token else None
        self.model = model.strip() if model else None
        self.timeout = timeout_seconds

    async def embed_query(self, text: str) -> Sequence[float]:
        """嵌入单条查询文本。

        参数：text: 查询文本
        返回：dimension 维的浮点向量
        异常：HTTP 错误、响应缺向量、维度不符时抛异常
        """
        vectors = await self.embed_documents([text])
        return vectors[0]

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """批量嵌入文档文本（阶段三：≤32 条/批、复用 client、严格校验）。

        参数：texts: 待嵌入文本序列
        返回：与输入等长的向量序列
        异常：任一文本嵌入失败（HTTP/维度/数量不符）时抛异常 ——
              批量导入要求"任一向量异常直接失败"，不静默降级
        """
        if not texts:
            return []
        results: list[Sequence[float]] = []
        # 复用单一 client：连接复用 + 统一超时；分批循环保证单批 ≤32
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for start in range(0, len(texts), self.EMBED_BATCH_SIZE):
                batch = list(texts[start : start + self.EMBED_BATCH_SIZE])
                headers = {"Content-Type": "application/json"}
                if self.auth_token:
                    headers["X-Embedding-Proxy-Token"] = self.auth_token
                request_payload: dict[str, object] = {"texts": batch}
                if self.model:
                    request_payload["model"] = self.model
                response = await client.post(
                    self.endpoint,
                    json=request_payload,
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("embedding 响应必须是 JSON 对象")
                embeddings = payload.get("embeddings") or payload.get("data") or []
                # 兼容 OpenAI embeddings 形态：data=[{index, embedding}, ...]。
                if isinstance(embeddings, list) and embeddings and all(
                    isinstance(item, dict) for item in embeddings
                ):
                    try:
                        indices = [int(item["index"]) for item in embeddings]
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeError("embedding data 项缺少有效 index/embedding") from exc
                    if any(isinstance(item.get("index"), bool) for item in embeddings):
                        raise RuntimeError("embedding data index 类型无效")
                    if sorted(indices) != list(range(len(batch))):
                        raise RuntimeError("embedding data index 不连续或重复")
                    embeddings = [
                        item["embedding"]
                        for item in sorted(embeddings, key=lambda item: int(item["index"]))
                    ]
                if len(embeddings) != len(batch):
                    raise RuntimeError(
                        f"embedding 返回数量 {len(embeddings)} 与请求数量 {len(batch)} 不一致"
                    )
                for vector in embeddings:
                    # 维度校验在写入 / 查询前拦截，防止维度错配的脏向量进入 pgvector
                    if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
                        raise ValueError("embedding 向量格式无效")
                    if len(vector) != self.dimension:
                        raise ValueError("embedding 维度与配置不一致")
                    values = [float(value) for value in vector]
                    if not all(math.isfinite(value) for value in values):
                        raise ValueError("embedding 向量包含非有限数")
                    results.append(values)
        return results


class PgVectorRetriever:
    """pgvector 向量检索器：写入嵌入 + 向量相似度检索（含强制 ACL）。"""

    def __init__(
        self,
        repository: KnowledgeRepository,
        embedder: EmbeddingProvider,
        *,
        dimension: int,
        min_similarity: float = 0.0,
    ) -> None:
        """构造检索器。

        参数：
            repository: 知识仓库（提供连接池与文档元数据）
            embedder: 查询嵌入提供方
            dimension: 向量维度（8..4096，须与列定义一致）
            min_similarity: 向量相似度（1 - 余弦距离）拒答阈值，[0,1]；
                低于阈值的命中不返回（用于「无答案」检索后拒答；
                默认 0.0 = 不过滤，保持既有行为）
        """
        # 维度范围与 PostgreSQL vector 类型的合理范围对齐，越界即配置错误
        if dimension < 8 or dimension > 4096:
            raise ValueError("向量维度必须在 8 到 4096 之间")
        if not 0.0 <= min_similarity <= 1.0:
            raise ValueError("min_similarity 必须在 0 到 1 之间")
        self.repository = repository
        self.embedder = embedder
        self.dimension = dimension
        self.min_similarity = min_similarity

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
                # （public 全可见 / internal·restricted 需 internal 主体）-> 部门白名单
                # -> 相似度拒答阈值（min_similarity，1 - 余弦距离）；
                # 距离用 <=>（余弦距离），窗口函数 row_number 生成来源内排名
                await cursor.execute(
                    """
                    SELECT c.tenant_id, c.document_id, c.document_version, c.chunk_id,
                           d.title, c.content, d.source_uri,
                           row_number() OVER (
                               ORDER BY c.embedding <=> %s::vector, c.document_id, c.chunk_id
                           ) AS source_rank,
                           1 - (c.embedding <=> %s::vector) AS similarity
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
                      AND 1 - (c.embedding <=> %s::vector) >= %s
                    ORDER BY c.embedding <=> %s::vector, c.document_id, c.chunk_id
                    LIMIT %s
                    """,
                    (
                        literal,
                        literal,
                        principal.tenant_id,
                        principal.internal,
                        principal.internal,
                        list(principal.departments),
                        list(principal.departments),
                        literal,
                        self.min_similarity,
                        literal,
                        limit,
                    ),
                )
                rows = await cursor.fetchall()
        # 构造不可变的检索命中；fused_score 由融合阶段（RRF）再填充。
        # similarity 在 SQL 中已返回，从 row 提出显式转换（numeric -> float）后
        # 从 **row 中移除，避免重复关键字。
        hits: list[RetrievalHit] = []
        for row in rows:
            raw_similarity = row.pop("similarity", None)
            similarity = (
                float(raw_similarity) if raw_similarity is not None else None
            )
            hits.append(
                RetrievalHit(
                    source="vector",
                    fused_score=0.0,
                    similarity=similarity,
                    **row,
                )
            )
        return hits
