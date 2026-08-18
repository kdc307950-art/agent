from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from src.my_agent.agent import build_agent

from .repositories import LongTermMemoryRepository
from .settings import Settings


@dataclass
class AgentRuntime:
    graph: object
    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore
    memory: LongTermMemoryRepository


@asynccontextmanager
async def runtime_context(settings: Settings) -> AsyncIterator[AgentRuntime]:
    async with AsyncExitStack() as stack:
        checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.database_url)
        )
        store = await stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(settings.database_url)
        )
        if settings.auto_setup:
            await checkpointer.setup()
            await store.setup()
        graph = build_agent(
            checkpointer=checkpointer,
            store=store,
            model_retry_attempts=settings.model_retry_attempts,
        )
        yield AgentRuntime(
            graph=graph,
            checkpointer=checkpointer,
            store=store,
            memory=LongTermMemoryRepository(store),
        )
