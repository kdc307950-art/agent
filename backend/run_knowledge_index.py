"""知识文档向量化导入 CLI —— 为已发布文档补齐 embedding（种子/文档可向量化）。

用法：

    # 未配置 embedding 端点：只统计 pending 数量（提示先配置）
    uv run python -m backend.run_knowledge_index --tenant demo

    # 配置 KNOWLEDGE_EMBEDDING_ENDPOINT / DIMENSION 后批量向量化
    uv run python -m backend.run_knowledge_index --tenant demo --batch-size 32 --embed

--embed 模式强制检查：endpoint 已配置、返回数量与输入一致、维度与配置一致、
向量为有限数；任一不满足直接失败，不静默降级。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import load_environment
from .knowledge import (
    HttpEmbeddingProvider,
    KnowledgeRepository,
    PgVectorRetriever,
    RetrievalPrincipal,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STATUS_PENDING = "pending"
STATUS_READY = "ready"


async def _pending_chunks(pool, tenant_id: str) -> list[dict]:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT c.tenant_id, c.document_id, c.document_version, c.chunk_id,
                       c.content, c.embedding_status, c.embedding_model,
                       d.allowed_departments
                FROM knowledge_chunks AS c
                JOIN knowledge_documents AS d
                  ON d.tenant_id = c.tenant_id
                 AND d.document_id = c.document_id
                 AND d.version = c.document_version
                WHERE c.tenant_id = %s
                  AND d.status = 'published'
                  AND (c.embedding_status IS NULL OR c.embedding_status <> 'ready')
                ORDER BY c.document_id, c.chunk_id
                """,
                (tenant_id,),
            )
            return list(await cursor.fetchall())


async def _mark_ready(pool, chunk: dict, model: str) -> None:
    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE knowledge_chunks
            SET embedding_status = 'ready', embedding_model = %s,
                embedding_updated_at = %s
            WHERE tenant_id = %s AND document_id = %s
              AND document_version = %s AND chunk_id = %s
            """,
            (
                model,
                datetime.now(UTC),
                chunk["tenant_id"],
                chunk["document_id"],
                chunk["document_version"],
                chunk["chunk_id"],
            ),
        )


async def _run(tenant_id: str, conninfo: str, *, batch_size: int, embed: bool) -> dict:
    if batch_size < 1 or batch_size > HttpEmbeddingProvider.EMBED_BATCH_SIZE:
        raise ValueError(
            f"batch_size 必须在 1 到 {HttpEmbeddingProvider.EMBED_BATCH_SIZE} 之间"
        )
    embedding_endpoint = os.getenv("KNOWLEDGE_EMBEDDING_ENDPOINT", "").strip()
    try:
        dimension = int(os.getenv("KNOWLEDGE_EMBEDDING_DIMENSION", "1536"))
    except ValueError as exc:
        raise SystemExit("KNOWLEDGE_EMBEDDING_DIMENSION 必须是整数") from exc
    embedding_model = os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "").strip() or None
    record_model = embedding_model or "unknown"
    embedding_token = os.getenv("KNOWLEDGE_EMBEDDING_TOKEN", "").strip() or None
    if embed and not embedding_endpoint:
        raise SystemExit("--embed 需要配置 KNOWLEDGE_EMBEDDING_ENDPOINT")

    pool = AsyncConnectionPool(conninfo, min_size=1, max_size=2, open=False, name="knowledge-index")
    await pool.open(wait=True)
    try:
        chunks = await _pending_chunks(pool, tenant_id)
        # 只有显式 --embed 才允许产生外部 embedding 请求；即使环境已配置
        # endpoint，默认命令也只做盘点，避免误触发付费调用。
        if not embed:
            return {"mode": "dry-run", "pending": len(chunks), "ready": 0}
        repository = KnowledgeRepository(pool)
        vector = PgVectorRetriever(
            repository,
            HttpEmbeddingProvider(
                embedding_endpoint,
                dimension=dimension,
                auth_token=embedding_token,
                model=embedding_model,
            ),
            dimension=dimension,
        )
        ready = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            texts = [str(chunk["content"]) for chunk in batch]
            # 文档导入优先走批量接口，避免逐 chunk 建立请求；保留单条回退以兼容
            # 外部注入的旧 EmbeddingProvider 实现。
            embed_documents = getattr(vector.embedder, "embed_documents", None)
            if callable(embed_documents):
                embeddings = await embed_documents(texts)
            else:
                embeddings = [await vector.embedder.embed_query(text) for text in texts]
            if len(embeddings) != len(batch):
                raise RuntimeError(f"embedding 返回数量 {len(embeddings)} != 输入数量 {len(batch)}")
            for chunk, embedding in zip(batch, embeddings, strict=True):
                if len(embedding) != dimension:
                    raise RuntimeError(
                        f"embedding 维度 {len(embedding)} != 配置 {dimension}（chunk {chunk['document_id']}/{chunk['chunk_id']}）"
                    )
                principal = RetrievalPrincipal(
                    tenant_id=chunk["tenant_id"],
                    departments=frozenset(chunk["allowed_departments"] or []),
                    internal=True,
                )
                ok = await vector.put_embedding(
                    principal,
                    document_id=chunk["document_id"],
                    document_version=chunk["document_version"],
                    chunk_id=chunk["chunk_id"],
                    embedding=embedding,
                    embedding_model=record_model,
                )
                if not ok:
                    raise RuntimeError(
                        f"写入 embedding 失败: {chunk['document_id']}/{chunk['chunk_id']}"
                    )
                await _mark_ready(pool, chunk, record_model)
                ready += 1
        return {"mode": "embed", "pending": len(chunks) - ready, "ready": ready}
    finally:
        await pool.close()


def main() -> None:
    load_environment()
    parser = argparse.ArgumentParser(description="知识文档向量化导入")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--embed", action="store_true", help="强制执行向量化（需 KNOWLEDGE_EMBEDDING_ENDPOINT）"
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    conninfo = args.database_url or os.getenv("DATABASE_URL", "").strip()
    if not conninfo:
        raise SystemExit("缺少 DATABASE_URL")
    result = asyncio.run(_run(args.tenant, conninfo, batch_size=args.batch_size, embed=args.embed))
    if result["mode"] == "dry-run":
        print(
            f"dry-run: 租户 {args.tenant} 有 {result['pending']} 个待向量化分块；配置 KNOWLEDGE_EMBEDDING_ENDPOINT 后加 --embed 执行"
        )
    else:
        print(f"embed 完成: ready={result['ready']} pending={result['pending']}")
        if result["pending"]:
            raise SystemExit(f"仍有 {result['pending']} 个分块未向量化")


if __name__ == "__main__":
    main()
