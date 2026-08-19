"""Application schema versioning and readiness checks.

LangGraph owns its checkpoint/store migration tables.  This module owns the
application tables and records the version that was explicitly applied by the
`backend.migrations` command.  The service refuses readiness when the version
or required relations do not match the code it is running.
"""

from __future__ import annotations

from typing import Iterable

from psycopg import AsyncConnection


APP_SCHEMA_NAME = "langgraph_agent"
APP_SCHEMA_VERSION = 2
MIGRATION_LOCK_KEY = 891274631

# These are the tables created by the pinned LangGraph PostgreSQL adapters and
# by backend.audit.  Keeping the list here makes schema drift visible at
# readiness time instead of on the first user request.
REQUIRED_RELATIONS: tuple[str, ...] = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "store_migrations",
    "store",
    "agent_runs",
    "agent_events",
    "agent_schema_version",
    "agent_thread_activity",
)


async def ensure_schema_version(connection: AsyncConnection) -> None:
    """Create or validate the application schema version row.

    Future schema changes must add an explicit migration step and increment
    ``APP_SCHEMA_VERSION``.  A running application never upgrades the schema.
    """

    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_schema_version (
                schema_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL CHECK (version >= 1),
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await cursor.execute(
            "SELECT version FROM agent_schema_version WHERE schema_name = %s",
            (APP_SCHEMA_NAME,),
        )
        row = await cursor.fetchone()
        if row is None:
            await cursor.execute(
                "INSERT INTO agent_schema_version (schema_name, version) VALUES (%s, %s)",
                (APP_SCHEMA_NAME, APP_SCHEMA_VERSION),
            )
            return
        current = int(row[0])
        if current > APP_SCHEMA_VERSION:
            raise RuntimeError(
                f"应用 schema 版本不匹配: database={current}, expected={APP_SCHEMA_VERSION}; "
                "请先运行迁移命令"
            )
        if current < APP_SCHEMA_VERSION:
            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_thread_activity (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    thread_id TEXT PRIMARY KEY,
                    last_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_finished_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_thread_activity_finished
                ON agent_thread_activity (last_finished_at)
                WHERE last_finished_at IS NOT NULL
                """
            )
            await cursor.execute(
                """
                INSERT INTO agent_thread_activity
                    (tenant_id, user_id, thread_id, last_started_at, last_finished_at, updated_at)
                SELECT tenant_id, user_id, thread_id, max(started_at), max(finished_at), now()
                FROM agent_runs
                GROUP BY tenant_id, user_id, thread_id
                ON CONFLICT (thread_id) DO NOTHING
                """
            )
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (APP_SCHEMA_VERSION, APP_SCHEMA_NAME),
            )


async def check_required_relations(
    connection: AsyncConnection,
    relations: Iterable[str] = REQUIRED_RELATIONS,
) -> list[str]:
    """Return missing public relations without exposing connection details."""

    names = tuple(relations)
    if not names:
        return []
    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            SELECT relname
            FROM pg_catalog.pg_class
            JOIN pg_catalog.pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE pg_namespace.nspname = 'public' AND relname = ANY(%s)
            """,
            (list(names),),
        )
        present = {str(row[0]) for row in await cursor.fetchall()}
    return [name for name in names if name not in present]


async def check_schema_ready(connection: AsyncConnection) -> None:
    """Raise a safe, actionable error when schema is absent or out of date."""

    missing = await check_required_relations(connection)
    if missing:
        raise RuntimeError("PostgreSQL schema 未完成迁移")
    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT version FROM agent_schema_version WHERE schema_name = %s",
            (APP_SCHEMA_NAME,),
        )
        row = await cursor.fetchone()
    if row is None or int(row[0]) != APP_SCHEMA_VERSION:
        raise RuntimeError("应用 schema 版本不匹配，请先运行迁移命令")
