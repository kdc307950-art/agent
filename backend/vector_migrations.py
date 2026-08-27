"""Explicit pgvector migration for the optional Agentic RAG vector backend."""

from __future__ import annotations

import asyncio
import os

from psycopg import AsyncConnection, sql

from .settings import database_url_from_env


async def setup_vector_schema(*, dimension: int | None = None) -> None:
    dimension = dimension or int(os.getenv("KNOWLEDGE_EMBEDDING_DIMENSION", "1536"))
    if dimension < 8 or dimension > 4096:
        raise RuntimeError("KNOWLEDGE_EMBEDDING_DIMENSION 必须在 8 到 4096 之间")
    async with await AsyncConnection.connect(database_url_from_env(), autocommit=True) as connection:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await connection.execute(
            sql.SQL("ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding vector({})").format(
                sql.Literal(dimension)
            )
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding_hnsw
            ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
            WHERE embedding IS NOT NULL
            """
        )


if __name__ == "__main__":
    asyncio.run(setup_vector_schema())
