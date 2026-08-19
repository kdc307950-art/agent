from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from psycopg import AsyncConnection
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from src.my_agent.agent import build_agent

from .audit import AuditRepository, audit_context
from .metrics import RuntimeMetrics
from .repositories import LongTermMemoryRepository
from .schema import check_schema_ready, ensure_schema_version
from .settings import Settings
from .tool_governance import ToolGovernance


@dataclass
class AgentRuntime:
    graph: object
    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore
    memory: LongTermMemoryRepository
    audit: AuditRepository
    tool_governance: ToolGovernance
    metrics: RuntimeMetrics


@asynccontextmanager
async def runtime_context(
    settings: Settings,
    *,
    metrics: RuntimeMetrics | None = None,
) -> AsyncIterator[AgentRuntime]:
    async with AsyncExitStack() as stack:
        checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.database_url)
        )
        store = await stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(settings.database_url)
        )
        audit = await stack.enter_async_context(
            audit_context(settings.database_url)
        )
        if settings.auto_setup:
            await checkpointer.setup()
            await store.setup()
            await audit.setup()
        async with await AsyncConnection.connect(settings.database_url) as connection:
            if settings.auto_setup:
                await ensure_schema_version(connection)
            await check_schema_ready(connection)
        await audit.check_ready()
        runtime_metrics = metrics or RuntimeMetrics()
        tool_governance = ToolGovernance(
            audit,
            tenant_allowlist=settings.tool_tenant_allowlist,
            max_retry_attempts=settings.tool_retry_attempts,
            metrics=runtime_metrics,
        )
        graph = build_agent(
            checkpointer=checkpointer,
            store=store,
            model_retry_attempts=settings.model_retry_attempts,
            api_key=settings.deepseek_api_key,
            base_url=settings.llm_base_url,
            model_name=settings.llm_model,
            tool_call_wrapper=tool_governance.awrap_tool_call,
        )
        yield AgentRuntime(
            graph=graph,
            checkpointer=checkpointer,
            store=store,
            memory=LongTermMemoryRepository(store),
            audit=audit,
            tool_governance=tool_governance,
            metrics=runtime_metrics,
        )
