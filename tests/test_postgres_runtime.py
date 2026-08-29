import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import Command, interrupt
from psycopg import AsyncConnection, sql
from typing_extensions import TypedDict

from backend.audit import audit_context
from backend.run_context import RunContext
from backend.runtime import runtime_context
from backend.schema import APP_SCHEMA_VERSION, ensure_schema_version
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


def test_schema_v2_migrates_status_constraint_and_helpdesk_tables_to_current_version():
    async def run():
        schema_name = f"migration_{uuid4().hex}"
        async with await AsyncConnection.connect(DATABASE_URL, autocommit=True) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
                )
            try:
                await connection.execute(
                    sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema_name))
                )
                await connection.execute("""
                    CREATE TABLE agent_runs (
                        run_id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        tenant_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'running', 'completed', 'timeout', 'cancelled',
                                'failed', 'budget_exceeded'
                            )
                        ),
                        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        finished_at TIMESTAMPTZ,
                        error_code TEXT,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """)
                await connection.execute("""
                    CREATE TABLE agent_schema_version (
                        schema_name TEXT PRIMARY KEY,
                        version INTEGER NOT NULL CHECK (version >= 1),
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """)
                await connection.execute(
                    "INSERT INTO agent_schema_version (schema_name, version) VALUES (%s, 2)",
                    ("langgraph_agent",),
                )

                await ensure_schema_version(connection)
                await connection.execute("""
                    INSERT INTO agent_runs
                        (run_id, request_id, tenant_id, user_id, thread_id, status)
                    VALUES ('run-1', 'request-1', 'tenant-a', 'user-1', 'thread-1', 'awaiting_approval')
                    """)
                version = await connection.execute(
                    "SELECT version FROM agent_schema_version WHERE schema_name = %s",
                    ("langgraph_agent",),
                )
                version_row = await version.fetchone()
                constraint = await connection.execute("""
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'agent_runs'::regclass
                      AND conname = 'agent_runs_status_check'
                    """)
                constraint_row = await constraint.fetchone()
                relations = await connection.execute("""
                    SELECT to_regclass('tickets'),
                           to_regclass('ticket_status_events'),
                           to_regclass('inbound_events'),
                           to_regclass('knowledge_documents'),
                           to_regclass('knowledge_chunks'),
                           to_regclass('ticket_messages'),
                           to_regclass('outbox_events'),
                           to_regclass('sla_policies'),
                           to_regclass('ticket_sla'),
                           to_regclass('satisfaction_surveys'),
                           to_regclass('ticket_workflow_runs'),
                           to_regclass('support_teams'),
                           to_regclass('support_members'),
                           to_regclass('support_schedules'),
                           to_regclass('routing_rules'),
                           to_regclass('ticket_assignments'),
                           to_regclass('it_assets'),
                           to_regclass('tenant_it_policies'),
                           to_regclass('admin_audit_events')
                    """)
                relation_row = await relations.fetchone()
                return int(version_row[0]), str(constraint_row[0]), tuple(relation_row)
            finally:
                await connection.execute("SET search_path TO public")
                await connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
                )

    version, constraint_definition, relations = asyncio.run(run())
    assert version == APP_SCHEMA_VERSION
    assert "awaiting_approval" in constraint_definition
    assert all(relation is not None for relation in relations)


def test_schema_v9_migrates_to_current_and_is_repeatable():
    """v9 → 当前版本迁移可重复执行：从 v9 版本号一次升到当前版本，
    再次对同一 schema 运行迁移不报错、版本号不变、关系仍在。"""

    async def run():
        schema_name = f"migration_v9_{uuid4().hex}"
        async with await AsyncConnection.connect(DATABASE_URL, autocommit=True) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
                )
            try:
                await connection.execute(
                    sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema_name))
                )
                await connection.execute("""
                    CREATE TABLE agent_schema_version (
                        schema_name TEXT PRIMARY KEY,
                        version INTEGER NOT NULL CHECK (version >= 1),
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """)
                await connection.execute(
                    "INSERT INTO agent_schema_version (schema_name, version) VALUES (%s, 9)",
                    ("langgraph_agent",),
                )

                # 第一次迁移：v9 → v11
                await ensure_schema_version(connection)
                first = await connection.execute(
                    "SELECT version FROM agent_schema_version WHERE schema_name = %s",
                    ("langgraph_agent",),
                )
                first_version = int((await first.fetchone())[0])

                # 第二次迁移：同一 schema 重跑，应幂等成功
                await ensure_schema_version(connection)
                second = await connection.execute(
                    "SELECT version FROM agent_schema_version WHERE schema_name = %s",
                    ("langgraph_agent",),
                )
                second_version = int((await second.fetchone())[0])

                relations = await connection.execute("""
                    SELECT to_regclass('tenant_it_policies'),
                           to_regclass('it_assets'),
                           to_regclass('admin_audit_events'),
                           to_regclass('tickets')
                    """)
                relation_row = await relations.fetchone()
                return first_version, second_version, tuple(relation_row)
            finally:
                await connection.execute("SET search_path TO public")
                await connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
                )

    first_version, second_version, relations = asyncio.run(run())
    assert first_version == APP_SCHEMA_VERSION
    assert second_version == APP_SCHEMA_VERSION
    assert all(relation is not None for relation in relations)


def test_postgres_interrupt_survives_reconnect_and_audit_runs_are_linked():
    class ApprovalState(TypedDict, total=False):
        approved: bool

    def approval_node(_state: ApprovalState) -> dict[str, bool]:
        decision = interrupt({"question": "是否批准？"})
        return {"approved": bool(decision["approved"])}

    async def run():
        thread_id = f"approval-{uuid4().hex}"
        first_run_id = f"run-{uuid4().hex}"
        resumed_run_id = f"run-{uuid4().hex}"
        config = {"configurable": {"thread_id": thread_id}}
        workflow = StateGraph(ApprovalState)
        workflow.add_node("approval", approval_node)
        workflow.add_edge(START, "approval")
        workflow.add_edge("approval", END)

        first_context = RunContext(
            run_id=first_run_id,
            request_id=f"request-{first_run_id}",
            tenant_id="tenant-a",
            user_id="user-1",
            thread_id=thread_id,
            scopes=frozenset({"chat:write", "chat:approve"}),
            deadline=asyncio.get_running_loop().time() + 60,
        )
        resumed_context = RunContext(
            run_id=resumed_run_id,
            request_id=f"request-{resumed_run_id}",
            tenant_id="tenant-a",
            user_id="approver-1",
            thread_id=thread_id,
            scopes=frozenset({"chat:write", "chat:approve"}),
            deadline=asyncio.get_running_loop().time() + 60,
        )

        async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
            await checkpointer.setup()
            graph = workflow.compile(checkpointer=checkpointer)
            first_result = await graph.ainvoke({}, config)
            snapshot = await graph.aget_state(config)
            interrupt_id = str(snapshot.tasks[0].interrupts[0].id)

        async with audit_context(DATABASE_URL) as audit:
            await audit.setup()
            await audit.start_run(first_context)
            await audit.finish_run(
                first_context,
                "awaiting_approval",
                metadata={"interrupt_id": interrupt_id},
            )

        async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
            graph = workflow.compile(checkpointer=checkpointer)
            restored = await graph.aget_state(config)
            resumed_result = await graph.ainvoke(Command(resume={"approved": True}), config)

        async with audit_context(DATABASE_URL) as audit:
            await audit.start_run(
                resumed_context,
                metadata={
                    "resumed": True,
                    "resumed_from": first_run_id,
                    "interrupt_id": interrupt_id,
                },
            )
            await audit.finish_run(resumed_context, "completed")
            first_run = await audit.get_run("tenant-a", first_run_id)
            resumed_run = await audit.get_run("tenant-a", resumed_run_id)

        return first_result, restored, resumed_result, first_run, resumed_run, interrupt_id

    first_result, restored, resumed_result, first_run, resumed_run, interrupt_id = asyncio.run(
        run()
    )
    assert "__interrupt__" in first_result
    assert restored.tasks[0].interrupts[0].id == interrupt_id
    assert resumed_result["approved"] is True
    assert first_run["status"] == "awaiting_approval"
    assert resumed_run["status"] == "completed"
    assert resumed_run["metadata"]["resumed_from"] == first_run["run_id"]
    assert resumed_run["metadata"]["interrupt_id"] == interrupt_id


def test_runtime_fails_explicitly_when_postgres_is_unreachable():
    settings = replace(
        Settings.from_env(),
        database_url="postgresql://invalid:invalid@127.0.0.1:1/missing?connect_timeout=1",
    )

    async def run():
        with pytest.raises(Exception):  # noqa: B017 - 意图是"任何异常都算显式失败"（连接不可达）
            await asyncio.wait_for(_enter_runtime(), timeout=3)

    async def _enter_runtime():
        async with runtime_context(settings):
            return True

    asyncio.run(run())
