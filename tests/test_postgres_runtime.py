import asyncio
import os
from uuid import uuid4

import pytest

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore


DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_postgres_schema_and_store_round_trip():
    async def run():
        namespace = ("integration-test", uuid4().hex)
        key = "probe"
        async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
            await checkpointer.setup()
        async with AsyncPostgresStore.from_conn_string(DATABASE_URL) as store:
            await store.setup()
            await store.aput(namespace, key, {"status": "ok"})
            item = await store.aget(namespace, key)
            await store.adelete(namespace, key)
        return item

    item = asyncio.run(run())
    assert item is not None
    assert item.value == {"status": "ok"}
