from __future__ import annotations

import asyncio

from psycopg import AsyncConnection
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from .audit import audit_context
from .schema import MIGRATION_LOCK_KEY, ensure_schema_version
from .settings import database_url_from_env


async def setup_postgres() -> None:
    database_url = database_url_from_env()
    # autocommit 是必需的，不是风格选择：
    # psycopg3 默认在第一条语句上隐式开启事务，于是这条连接会一直停在
    # `idle in transaction` 并持有 advisory lock。而 checkpointer.setup() 里的
    # `CREATE INDEX CONCURRENTLY` 必须等待所有并发事务结束 —— 它等的就是这条连接，
    # 而这条连接又在等 setup() 返回，迁移直接死锁。
    #
    # 这个死锁只在**全新空库**上出现：库里已有索引时 `IF NOT EXISTS` 立刻返回，
    # 不进入等待。也就是说它只在首次部署和干净 CI 环境里触发。
    #
    # advisory lock 用的是 session 级的 pg_advisory_lock，autocommit 下同样持有到
    # 显式 unlock 或连接断开，互斥语义不受影响。
    async with await AsyncConnection.connect(database_url, autocommit=True) as lock_connection:
        async with lock_connection.cursor() as cursor:
            await cursor.execute("SELECT pg_advisory_lock(%s::bigint)", (MIGRATION_LOCK_KEY,))
        try:
            async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
                await checkpointer.setup()
            async with AsyncPostgresStore.from_conn_string(database_url) as store:
                await store.setup()
            async with audit_context(database_url) as audit:
                await audit.setup()
            await ensure_schema_version(lock_connection)
        finally:
            async with lock_connection.cursor() as cursor:
                await cursor.execute("SELECT pg_advisory_unlock(%s::bigint)", (MIGRATION_LOCK_KEY,))


if __name__ == "__main__":
    asyncio.run(setup_postgres())
