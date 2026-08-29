"""pgvector 显式迁移 —— 为可选的 Agentic RAG 向量后端准备数据库结构。

职责：
    - 安装 vector / pg_trgm 扩展
    - 为 knowledge_chunks 表补充 embedding 向量列（维度可配置）
    - 创建 HNSW 近似最近邻索引，加速向量相似度检索

关键设计：
    - 幂等迁移：CREATE EXTENSION / ADD COLUMN IF NOT EXISTS /
      CREATE INDEX IF NOT EXISTS，可安全重复执行（与 schema v12 一致）
    - 维度校验：KNOWLEDGE_EMBEDDING_DIMENSION 必须在 8~4096（默认 1536，
      与 OpenAI text-embedding-3-small 对齐），防止误配置
    - 显式执行：不作为自动迁移的一部分，由运维按需运行
      （python -m backend.vector_migrations）
"""

from __future__ import annotations

import asyncio
import os

from psycopg import AsyncConnection, sql

from .settings import database_url_from_env


async def setup_vector_schema(*, dimension: int | None = None) -> None:
    """安装 pgvector 扩展并创建向量列与 HNSW 索引（幂等）。

    参数：dimension 向量维度；未传时读环境变量 KNOWLEDGE_EMBEDDING_DIMENSION
    （默认 1536）。
    抛错：RuntimeError —— 维度超出 8~4096 范围。
    设计：连接以 autocommit=True 打开（DDL 不需要事务包裹），执行完自动关闭。
    """
    dimension = dimension or int(os.getenv("KNOWLEDGE_EMBEDDING_DIMENSION", "1536"))
    if dimension < 8 or dimension > 4096:
        raise RuntimeError("KNOWLEDGE_EMBEDDING_DIMENSION 必须在 8 到 4096 之间")
    async with await AsyncConnection.connect(
        database_url_from_env(), autocommit=True
    ) as connection:
        # HNSW 索引依赖 vector 类型，必须先确保扩展存在。
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # 中文检索兜底（与 schema v12 幂等一致）。
        await connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        # 向量列：knowledge_chunks 表存储文档分块，embedding 为可空向量列。
        await connection.execute(
            sql.SQL(
                "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding vector({})"
            ).format(sql.Literal(dimension))
        )
        # HNSW 索引（余弦距离）：embedding 非空的行才建索引，减少索引体积。
        await connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding_hnsw
            ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
            WHERE embedding IS NOT NULL
            """)


if __name__ == "__main__":
    asyncio.run(setup_vector_schema())
