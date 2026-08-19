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
    async with await AsyncConnection.connect(database_url) as lock_connection:
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
