import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from typing_extensions import TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.postgres.aio import AsyncPostgresStore

from backend.runtime import runtime_context
from backend.settings import Settings


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


def test_postgres_checkpoint_survives_runtime_restart():
    class CounterState(TypedDict):
        count: int

    async def increment(state: CounterState) -> dict[str, int]:
        return {"count": state["count"] + 1}

    async def run():
        thread_id = f"restart-{uuid4().hex}"
        config = {"configurable": {"thread_id": thread_id}}
        workflow = StateGraph(CounterState)
        workflow.add_node("increment", increment)
        workflow.add_edge(START, "increment")
        workflow.add_edge("increment", END)

        async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
            await checkpointer.setup()
            graph = workflow.compile(checkpointer=checkpointer)
            await graph.ainvoke({"count": 1}, config)

        async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
            graph = workflow.compile(checkpointer=checkpointer)
            state = await graph.aget_state(config)
        return state.values

    assert asyncio.run(run()) == {"count": 2}


def test_runtime_fails_explicitly_when_postgres_is_unreachable():
    settings = replace(
        Settings.from_env(),
        database_url="postgresql://invalid:invalid@127.0.0.1:1/missing?connect_timeout=1",
    )

    async def run():
        with pytest.raises(Exception):
            await asyncio.wait_for(_enter_runtime(), timeout=3)

    async def _enter_runtime():
        async with runtime_context(settings):
            return True

    asyncio.run(run())
