"""审计模块 —— 记录每次 Agent 运行的敏感操作日志。

职责：
    - sanitize_payload: 对写入审计的 payload 脱敏（隐藏 token/密钥等敏感字段）
    - AuditRepository:  审计记录落库（Postgres），提供写入与查询
    - NoopAuditRepository: 空实现（审计不可用时降级）
    - audit_context:    按数据库连接串创建审计仓库的异步上下文

设计：
    审计与业务解耦，写入失败不阻断主流程（有超时保护）。
"""

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
    """
    CREATE TABLE IF NOT EXISTS agent_thread_activity (
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        thread_id TEXT PRIMARY KEY,
        last_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_finished_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_thread_activity_finished
    ON agent_thread_activity (last_finished_at)
    WHERE last_finished_at IS NOT NULL
    """,
)

# prompt/content/result 是 LLM 特有的敏感字段，不是标准 auth 字段：
# prompt 含用户原文，content 是模型输出，result 是工具返回值——三者都不应进审计库明文。
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
    # 超长时保留原始内容的 sha256，而不是直接丢弃：
    # 审计系统可用哈希关联到其他日志系统（如 LangSmith）里的完整记录。
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
                # ON CONFLICT DO UPDATE：同一个 thread 可以跨多次 run 被复用（多轮对话），
                # 不能因 thread_id 已存在就报 unique 冲突——UPSERT 记录最新活跃时间即可。
                await cursor.execute(
                    """
                    INSERT INTO agent_thread_activity
                        (tenant_id, user_id, thread_id, last_started_at, updated_at)
                    VALUES (%s, %s, %s, now(), now())
                    ON CONFLICT (thread_id) DO UPDATE SET
                        tenant_id = EXCLUDED.tenant_id,
                        user_id = EXCLUDED.user_id,
                        last_started_at = EXCLUDED.last_started_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (context.tenant_id, context.user_id, context.thread_id),
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
                # WHERE 同时带 tenant_id：单靠 run_id 不足以隔离——
                # A 租户的 run_id 碰巧和 B 租户相同时，不应能改 B 的状态。
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
                        UPDATE agent_thread_activity
                        SET last_finished_at = now(), updated_at = now()
                        WHERE thread_id = %s AND tenant_id = %s
                        """,
                        (context.thread_id, context.tenant_id),
                    )
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
                # JOIN agent_runs 是隔离机制的一部分：event 只属于该租户的 run，
                # 即使攻击者猜到 run_id，跨租户查询也因 tenant_id 不匹配而返回空。
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
