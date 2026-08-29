"""Resolution Copilot 持久化 —— copilot_runs / copilot_drafts 表访问。

职责：
    - start_run：POST 入队（queued），operation_id 幂等
    - claim_copilot_run / complete_copilot_run / fail_copilot_run：Worker 领取/完成/失败
    - save_draft / get_latest_draft / get_draft_by_run：草稿读写
    - approve_draft / reject_draft / expire_drafts：草稿审批状态机
    - recover_orphaned_runs：超租约 processing 僵尸运行回队（queued）或 expired

关键设计（阶段二：异步 Worker 化）：
    - POST 只创建 queued 运行，模型执行由 CopilotWorker 异步完成（Web 进程不调模型）
    - 领取用 FOR UPDATE SKIP LOCKED + 租约（lease）+ worker_id，支持多副本并发
    - 临时错误指数退避（failed + next_attempt_at），超过重试次数 -> dead
    - 租约过期的 processing 由 recover_orphaned_runs 回队，崩溃后任务可恢复
    - operation_id 唯一约束：同 (tenant, ticket, operation) 不重复生成
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

# 运行状态（阶段二）
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"
STATUS_EXPIRED = "expired"


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
        """POST 入队：创建 queued 运行；同 (tenant, ticket, operation) 重复返回 False。

        幂等语义：expired/dead 允许重新运行（重置为 queued），
        completed/failed/processing 则拒绝重复登记。
        """
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO copilot_runs (
                        run_id, tenant_id, ticket_id, agent_name, status, operation_id,
                        lease_expires_at, heartbeat_at, attempts
                    ) VALUES (%s, %s, %s, %s, 'queued', %s, now() + (%s * interval '1 second'), now(), 0)
                    ON CONFLICT (tenant_id, ticket_id, operation_id) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        agent_name = EXCLUDED.agent_name,
                        status = 'queued',
                        error_code = NULL,
                        tool_calls = 0,
                        latency_ms = NULL,
                        worker_id = NULL,
                        lease_expires_at = now() + (%s * interval '1 second'),
                        heartbeat_at = now(),
                        next_attempt_at = NULL,
                        attempts = copilot_runs.attempts + 1,
                        completed_at = NULL
                    WHERE copilot_runs.status IN ('expired', 'dead')
                    """,
                    (run_id, tenant_id, ticket_id, agent_name, operation_id, lease_seconds, lease_seconds),
                )
                return cursor.rowcount == 1

    async def get_run_by_operation(
        self, tenant_id: str, ticket_id: str, operation_id: str
    ) -> dict[str, Any] | None:
        """按 operation_id 查询已有运行（幂等判断用）。"""
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT run_id, status, error_code, tool_calls, started_at, completed_at,
                           lease_expires_at, heartbeat_at, attempts, worker_id, next_attempt_at
                    FROM copilot_runs
                    WHERE tenant_id = %s AND ticket_id = %s AND operation_id = %s
                    """,
                    (tenant_id, ticket_id, operation_id),
                )
                return await cursor.fetchone()

    async def get_run(
        self, tenant_id: str, run_id: str
    ) -> dict[str, Any] | None:
        """按 run_id 查询运行（GET /copilot/{run_id} 状态查询用）。"""
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT run_id, tenant_id, ticket_id, status, error_code, tool_calls,
                           attempts, started_at, completed_at, started_at AS created_at, worker_id
                    FROM copilot_runs
                    WHERE tenant_id = %s AND run_id = %s
                    """,
                    (tenant_id, run_id),
                )
                return await cursor.fetchone()

    async def claim_copilot_runs(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Worker 领取待处理运行（queued / 可重试 failed / 租约过期的 processing）。

        与 Outbox 同款 FOR UPDATE SKIP LOCKED，支持多 Worker 副本并发。
        """
        if not worker_id or lease_seconds < 1 or limit < 1 or limit > 20:
            raise ValueError("Copilot 领取参数无效")
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    WITH ready AS (
                        SELECT tenant_id, run_id, status AS previous_status
                        FROM copilot_runs
                        WHERE (
                            (status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
                            OR (status = 'failed' AND next_attempt_at IS NOT NULL AND next_attempt_at <= now())
                            OR (status = 'processing' AND lease_expires_at < now())
                        )
                        ORDER BY started_at, run_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE copilot_runs AS r
                    SET status = 'processing', claimed_at = now(),
                        attempts = attempts + 1, worker_id = %s,
                        lease_expires_at = now() + (%s * interval '1 second'),
                        heartbeat_at = now(), error_code = NULL
                    FROM ready
                    WHERE r.tenant_id = ready.tenant_id AND r.run_id = ready.run_id
                    RETURNING r.*, (ready.previous_status = 'processing') AS lease_recovered
                    """,
                    (limit, worker_id, lease_seconds),
                )
                return list(await cursor.fetchall())

    async def renew_run_lease(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool:
        """续租运行（processing 状态下刷新租约与心跳），供长执行中定期调用。"""
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_runs
                    SET lease_expires_at = now() + (%s * interval '1 second'),
                        heartbeat_at = now()
                    WHERE tenant_id = %s AND run_id = %s AND status = 'processing'
                      AND worker_id = %s AND lease_expires_at >= now()
                    """,
                    (lease_seconds, tenant_id, run_id, worker_id),
                )
                return cursor.rowcount == 1

    async def complete_copilot_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        tool_calls: int,
        latency_ms: int,
    ) -> bool:
        """Worker 成功完成运行：processing -> completed。"""
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_runs
                    SET status = 'completed', completed_at = now(), tool_calls = %s,
                        latency_ms = %s, error_code = NULL, worker_id = NULL,
                        lease_expires_at = NULL
                    WHERE tenant_id = %s AND run_id = %s AND status = 'processing'
                      AND worker_id = %s AND lease_expires_at >= now()
                    """,
                    (tool_calls, latency_ms, tenant_id, run_id, worker_id),
                )
                return cursor.rowcount == 1

    async def fail_copilot_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        error_code: str,
        retry_at: datetime | None,
        max_attempts: int = 3,
    ) -> bool:
        """Worker 失败处理：可重试 -> failed + next_attempt_at；超限 -> dead。"""
        target = STATUS_DEAD if retry_at is None else STATUS_FAILED
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_runs
                    SET status = %s, error_code = %s, completed_at = now(),
                        worker_id = NULL, lease_expires_at = NULL,
                        next_attempt_at = COALESCE(%s, next_attempt_at)
                    WHERE tenant_id = %s AND run_id = %s AND status = 'processing'
                      AND worker_id = %s AND lease_expires_at >= now()
                    """,
                    (target, error_code, retry_at, tenant_id, run_id, worker_id),
                )
                return cursor.rowcount == 1

    async def recover_orphaned_runs(
        self,
        *,
        lease_seconds: int = 60,
        max_recover: int = 20,
        now=None,
    ) -> int:
        """把超租约的 processing 僵尸运行回队（queued）或标记 expired。

        Worker 崩溃后任务可恢复：processing + 租约过期 -> queued（可被重新领取）；
        超过最大恢复阈值（attempts 过高）标记 expired 避免无限重跑。
        返回被恢复的运行数。
        """
        reference = now or datetime.now(UTC)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_runs AS r
                    SET status = CASE
                            WHEN r.attempts >= %s THEN 'expired'
                            ELSE 'queued'
                        END,
                        error_code = CASE
                            WHEN r.attempts >= %s THEN 'copilot_recovery_exhausted'
                            ELSE 'copilot_lease_recovered'
                        END,
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        next_attempt_at = now()
                    WHERE (r.tenant_id, r.run_id) IN (
                        SELECT sub.tenant_id, sub.run_id
                        FROM copilot_runs AS sub
                        WHERE sub.status = 'processing' AND sub.lease_expires_at < %s
                        ORDER BY sub.started_at, sub.run_id
                        LIMIT %s
                    )
                    """,
                    (max_recover, max_recover, reference, max_recover),
                )
                return cursor.rowcount

    # ========== 兼容旧同步入口（Worker 化前语义） ==========

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
        """直接更新运行终态（测试/旧调用兼容；生产由 Worker 方法驱动）。"""
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE copilot_runs
                    SET status = %s, completed_at = now(), tool_calls = %s,
                        latency_ms = %s, error_code = %s, worker_id = NULL,
                        lease_expires_at = NULL
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
                    (reference - timedelta(days=older_than_days),),
                )
                return cursor.rowcount
