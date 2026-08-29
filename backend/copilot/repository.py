"""Resolution Copilot 持久化 —— copilot_runs / copilot_drafts 表访问。

职责：
    - start_run / finish_run：记录每次 Agent 执行（含工具调用数、耗时、错误码）
    - save_draft：保存生成草稿
    - get_latest_draft：查询工单最新草稿
    - approve_draft / reject_draft / expire_drafts：草稿审批状态机
    - operation_id 幂等：同 (tenant, ticket, operation) 只允许一次生成

关键设计：
    - operation_id 唯一约束 + ON CONFLICT DO NOTHING 实现幂等；
      重复调用返回已有 run 记录，不重复生成、不重复消耗模型
    - 草稿与工单主表解耦：多次生成/审批/重试都可审计
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class CopilotRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def start_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        ticket_id: str,
        operation_id: str,
        agent_name: str = "resolution_copilot",
        lease_seconds: int = 60,
    ) -> bool:
        """登记一次 Copilot 运行；同 (tenant, ticket, operation) 重复返回 False（幂等）。

        lease_expires_at 用于识别僵尸运行（进程崩溃后 running 超租约）。
        状态机（阶段五）：expired 运行允许重新运行——同一 operation_id 的
        expired 记录被重置为 running（新 run_id），等价于"允许新的 operation 重试"。
        """
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO copilot_runs (
                        run_id, tenant_id, ticket_id, agent_name, status, operation_id,
                        lease_expires_at, heartbeat_at, attempts
                    ) VALUES (%s, %s, %s, %s, 'running', %s, now() + (%s * interval '1 second'), now(), 0)
                    ON CONFLICT (tenant_id, ticket_id, operation_id) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        agent_name = EXCLUDED.agent_name,
                        status = 'running',
                        error_code = NULL,
                        tool_calls = 0,
                        latency_ms = NULL,
                        lease_expires_at = now() + (%s * interval '1 second'),
                        heartbeat_at = now(),
                        attempts = copilot_runs.attempts + 1,
                        completed_at = NULL
                    WHERE copilot_runs.status = 'expired'
                    """,
                    (run_id, tenant_id, ticket_id, agent_name, operation_id, lease_seconds, lease_seconds),
                )
                return cursor.rowcount == 1

    async def get_run_by_operation(
        self, tenant_id: str, ticket_id: str, operation_id: str
    ) -> dict[str, Any] | None:
        """按 operation_id 查询已有运行（幂等判断用），含租约与尝试次数。"""
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT run_id, status, error_code, tool_calls, started_at, completed_at,
                           lease_expires_at, heartbeat_at, attempts
                    FROM copilot_runs
                    WHERE tenant_id = %s AND ticket_id = %s AND operation_id = %s
                    """,
                    (tenant_id, ticket_id, operation_id),
                )
                return await cursor.fetchone()

    async def renew_run_lease(
        self,
        *,
        tenant_id: str,
        run_id: str,
        lease_seconds: int = 60,
    ) -> bool:
        """续租运行（running 状态下刷新租约与心跳），供执行中定期调用。"""
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_runs
                    SET lease_expires_at = now() + (%s * interval '1 second'),
                        heartbeat_at = now()
                    WHERE tenant_id = %s AND run_id = %s AND status = 'running'
                    """,
                    (lease_seconds, tenant_id, run_id),
                )
                return cursor.rowcount == 1

    async def recover_expired_runs(
        self,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 2,
        now=None,
    ) -> int:
        """把超租约的 running 僵尸运行标记为 expired（阶段五状态机）。

        返回被恢复的运行数；expired 运行允许同一 operation_id 重新运行
        （start_run 的 ON CONFLICT 分支重置为 running）。
        """
        reference = now or datetime.now(UTC)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_runs
                    SET status = 'expired',
                        error_code = 'copilot_lease_expired',
                        completed_at = now()
                    WHERE status = 'running'
                      AND lease_expires_at < %s
                    """,
                    (reference,),
                )
                return cursor.rowcount

    async def finish_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        status: str,
        tool_calls: int,
        latency_ms: int,
        error_code: str | None = None,
    ) -> None:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_runs
                    SET status = %s, completed_at = now(), tool_calls = %s,
                        latency_ms = %s, error_code = %s
                    WHERE run_id = %s AND tenant_id = %s
                    """,
                    (status, tool_calls, latency_ms, error_code, run_id, tenant_id),
                )

    async def save_draft(
        self,
        *,
        draft_id: str,
        tenant_id: str,
        ticket_id: str,
        run_id: str,
        draft_answer: str | None,
        steps: list[str],
        citations: list[dict[str, Any]],
        confidence: float,
        needs_human_review: bool,
    ) -> None:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO copilot_drafts (
                        draft_id, tenant_id, ticket_id, run_id, draft_answer,
                        steps, citations, confidence, needs_human_review
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        draft_id,
                        tenant_id,
                        ticket_id,
                        run_id,
                        draft_answer,
                        Jsonb(steps),
                        Jsonb(citations),
                        confidence,
                        needs_human_review,
                    ),
                )

    async def get_latest_draft(
        self, tenant_id: str, ticket_id: str
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT draft_id, tenant_id, ticket_id, run_id, draft_answer,
                           steps, citations, confidence, needs_human_review,
                           status, created_at, approved_by, approved_at
                    FROM copilot_drafts
                    WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY created_at DESC, draft_id DESC
                    LIMIT 1
                    """,
                    (tenant_id, ticket_id),
                )
                return await cursor.fetchone()

    async def get_draft_by_run(
        self, tenant_id: str, ticket_id: str, run_id: str
    ) -> dict[str, Any] | None:
        """按 run_id 查询草稿（幂等恢复的唯一依据，不用 get_latest_draft 跨 run 取）。"""
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT draft_id, tenant_id, ticket_id, run_id, draft_answer,
                           steps, citations, confidence, needs_human_review,
                           status, created_at, approved_by, approved_at
                    FROM copilot_drafts
                    WHERE tenant_id = %s AND ticket_id = %s AND run_id = %s
                    ORDER BY created_at DESC, draft_id DESC
                    LIMIT 1
                    """,
                    (tenant_id, ticket_id, run_id),
                )
                return await cursor.fetchone()

    async def approve_draft(
        self,
        *,
        tenant_id: str,
        draft_id: str,
        approved_by: str,
        expected_status: str = "generated",
    ) -> bool:
        """审批通过草稿（generated -> approved）。

        只允许从 generated/reviewing 迁移；已审批/已拒绝/已过期的草稿不可再审批。
        """
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_drafts
                    SET status = 'approved', approved_by = %s, approved_at = now()
                    WHERE tenant_id = %s AND draft_id = %s
                      AND status IN ('generated', 'reviewing')
                    """,
                    (approved_by, tenant_id, draft_id),
                )
                return cursor.rowcount == 1

    async def reject_draft(self, *, tenant_id: str, draft_id: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_drafts
                    SET status = 'rejected', approved_at = now()
                    WHERE tenant_id = %s AND draft_id = %s
                      AND status IN ('generated', 'reviewing')
                    """,
                    (tenant_id, draft_id),
                )
                return cursor.rowcount == 1

    async def expire_drafts(self, *, older_than_days: int = 30, now=None) -> int:
        """批量过期未处理草稿（generated/reviewing 超过期限）。"""
        reference = now or datetime.now(UTC)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_drafts SET status = 'expired'
                    WHERE status IN ('generated', 'reviewing')
                      AND created_at < %s
                    """,
                    (reference - __import__("datetime").timedelta(days=older_than_days),),
                )
                return cursor.rowcount
