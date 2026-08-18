from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from psycopg.types.json import Jsonb
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .run_context import RunContext


logger = logging.getLogger("langgraph.audit")

AUDIT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'timeout', 'cancelled', 'failed', 'budget_exceeded')),
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        error_code TEXT,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_started
    ON agent_runs (tenant_id, started_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_events (
        id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        tenant_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        tool_name TEXT,
        status TEXT,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_events_run
    ON agent_events (run_id, occurred_at)
    """,
)

_SENSITIVE_KEY = re.compile(
    r"authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|prompt|content|result",
    re.IGNORECASE,
)


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def sanitize_payload(payload: dict[str, Any] | None, *, max_chars: int = 2048) -> dict[str, Any]:
    """Return bounded JSON-safe audit metadata with common secrets redacted."""
    safe = _redact(payload or {})
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= max_chars:
        return safe
    return {
        "truncated": True,
        "size_chars": len(encoded),
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


class AuditRepository:
    """Tenant-scoped PostgreSQL run and event repository."""

    def __init__(self, pool: AsyncConnectionPool, *, payload_limit: int = 2048) -> None:
        self.pool = pool
        self.payload_limit = payload_limit

    @classmethod
    async def connect(
        cls,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        payload_limit: int = 2048,
    ) -> "AuditRepository":
        pool = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            open=False,
            name="agent-audit",
        )
        await pool.open(wait=True)
        return cls(pool, payload_limit=payload_limit)

    async def close(self) -> None:
        await self.pool.close()

    async def setup(self) -> None:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                for statement in AUDIT_SCHEMA_STATEMENTS:
                    await cursor.execute(statement)

    async def check_ready(self) -> None:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT to_regclass('public.agent_runs'), to_regclass('public.agent_events')"
                )
                runs, events = await cursor.fetchone()
        if runs is None or events is None:
            raise RuntimeError("审计表未初始化，请先运行: uv run python -m backend.migrations")

    async def start_run(self, context: RunContext, *, metadata: dict[str, Any] | None = None) -> None:
        safe_metadata = sanitize_payload(metadata, max_chars=self.payload_limit)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO agent_runs
                        (run_id, request_id, tenant_id, user_id, thread_id, status, metadata)
                    VALUES (%s, %s, %s, %s, %s, 'running', %s)
                    """,
                    (
                        context.run_id,
                        context.request_id,
                        context.tenant_id,
                        context.user_id,
                        context.thread_id,
                        Jsonb(safe_metadata),
                    ),
                )
                await cursor.execute(
                    """
                    INSERT INTO agent_events (run_id, tenant_id, event_type, status, payload)
                    VALUES (%s, %s, 'run_started', 'running', %s)
                    """,
                    (context.run_id, context.tenant_id, Jsonb(safe_metadata)),
                )

    async def finish_run(
        self,
        context: RunContext,
        status: str,
        *,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if status not in {"completed", "timeout", "cancelled", "failed", "budget_exceeded"}:
            raise ValueError("无效的运行结束状态")
        safe_metadata = sanitize_payload(metadata, max_chars=self.payload_limit)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE agent_runs
                    SET status = %s, finished_at = now(), error_code = %s,
                        metadata = metadata || %s
                    WHERE run_id = %s AND tenant_id = %s
                    """,
                    (
                        status,
                        error_code,
                        Jsonb(safe_metadata),
                        context.run_id,
                        context.tenant_id,
                    ),
                )
                updated = cursor.rowcount == 1
                if updated:
                    await cursor.execute(
                        """
                        INSERT INTO agent_events
                            (run_id, tenant_id, event_type, status, payload)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            context.run_id,
                            context.tenant_id,
                            f"run_{status}",
                            status,
                            Jsonb({"error_code": error_code, **safe_metadata}),
                        ),
                    )
        return updated

    async def record_event(
        self,
        context: RunContext,
        event_type: str,
        *,
        tool_name: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        safe_payload = sanitize_payload(payload, max_chars=self.payload_limit)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO agent_events
                        (run_id, tenant_id, event_type, tool_name, status, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        context.run_id,
                        context.tenant_id,
                        event_type,
                        tool_name,
                        status,
                        Jsonb(safe_payload),
                    ),
                )

    async def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT run_id, request_id, tenant_id, user_id, thread_id,
                           status, started_at, finished_at, error_code, metadata
                    FROM agent_runs
                    WHERE run_id = %s AND tenant_id = %s
                    """,
                    (run_id, tenant_id),
                )
                return await cursor.fetchone()

    async def list_events(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT e.id, e.run_id, e.tenant_id, e.event_type, e.tool_name,
                           e.status, e.occurred_at, e.payload
                    FROM agent_events AS e
                    JOIN agent_runs AS r ON r.run_id = e.run_id AND r.tenant_id = e.tenant_id
                    WHERE e.run_id = %s AND e.tenant_id = %s
                    ORDER BY e.id ASC
                    """,
                    (run_id, tenant_id),
                )
                return list(await cursor.fetchall())


class NoopAuditRepository:
    """Compatibility fallback for unit-test app fixtures without a runtime."""

    async def start_run(self, *_args, **_kwargs) -> None:
        return None

    async def finish_run(self, *_args, **_kwargs) -> bool:
        return True

    async def record_event(self, *_args, **_kwargs) -> None:
        return None

    async def get_run(self, *_args, **_kwargs):
        return None

    async def list_events(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        return []


@asynccontextmanager
async def audit_context(conninfo: str, *, payload_limit: int = 2048) -> AsyncIterator[AuditRepository]:
    repository = await AuditRepository.connect(conninfo, payload_limit=payload_limit)
    try:
        yield repository
    finally:
        await repository.close()
