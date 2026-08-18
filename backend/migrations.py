from __future__ import annotations

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from .settings import database_url_from_env


async def setup_postgres() -> None:
    database_url = database_url_from_env()
    async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
        await checkpointer.setup()
    async with AsyncPostgresStore.from_conn_string(database_url) as store:
        await store.setup()


if __name__ == "__main__":
    asyncio.run(setup_postgres())
