"""Transactional messages, outbox delivery, SLA instances, and surveys."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .sla import BusinessCalendar


class OperationsConflict(RuntimeError):
    pass


def sla_policy_candidates(category: str | None) -> tuple[str, ...]:
    """从完整分类推导 SLA 策略匹配键，逐级回退。

    "it.vpn" -> ("it.vpn", "it");"it" -> ("it",);None -> ()。
    与受理图中 it_policy_provider 的候选顺序一致（先精确子分类，再父分类）。
    """
    if not category:
        return ()
    parts = category.split(".")
    return tuple(".".join(parts[:i]) for i in range(len(parts), 0, -1))


class TicketOperationsRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @classmethod
    async def connect(
        cls,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
    ) -> "TicketOperationsRepository":
        pool = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            open=False,
            name="helpdesk-operations-worker",
        )
        await pool.open(wait=True)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def append_outbound_message(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        message_id: str,
        actor_type: str,
        actor_id: str,
        channel: str,
        content: str,
        event_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> bool:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO outbox_events (
                        tenant_id, event_id, idempotency_key, event_type,
                        aggregate_type, aggregate_id, payload
                    ) VALUES (%s, %s, %s, 'ticket_message.send', 'ticket', %s, %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING event_id
                    """,
                    (tenant_id, event_id, idempotency_key, ticket_id, Jsonb(payload)),
                )
                created = await cursor.fetchone()
                if created is None:
                    return False
                await cursor.execute(
                    """
                    INSERT INTO ticket_messages (
                        tenant_id, ticket_id, message_id, direction, actor_type,
                        actor_id, channel, content
                    ) VALUES (%s, %s, %s, 'outbound', %s, %s, %s, %s)
                    """,
                    (tenant_id, ticket_id, message_id, actor_type, actor_id, channel, content),
                )
                return True

    async def claim_outbox(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        limit: int = 20,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not worker_id or lease_seconds < 1 or limit < 1 or limit > 100:
            raise ValueError("Outbox 租约参数无效")
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    WITH ready AS (
                        SELECT tenant_id, event_id, status AS previous_status
                        FROM outbox_events
                        WHERE (
                            (status = 'pending' AND available_at <= now())
                            OR (status = 'processing' AND lease_expires_at < now())
                        ) AND (%s::TEXT IS NULL OR tenant_id = %s)
                        ORDER BY available_at, created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE outbox_events AS o
                    SET status = 'processing', claimed_at = now(), attempts = attempts + 1,
                        worker_id = %s, lease_expires_at = now() + (%s * interval '1 second')
                    FROM ready
                    WHERE o.tenant_id = ready.tenant_id AND o.event_id = ready.event_id
                    RETURNING o.*, (ready.previous_status = 'processing') AS lease_recovered
                    """,
                    (tenant_id, tenant_id, limit, worker_id, lease_seconds),
                )
                return list(await cursor.fetchall())

    async def renew_outbox_lease(self, tenant_id: str, event_id: str, *, worker_id: str, lease_seconds: int = 60) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE outbox_events
                    SET lease_expires_at = now() + (%s * interval '1 second')
                    WHERE tenant_id = %s AND event_id = %s AND status = 'processing'
                      AND worker_id = %s AND lease_expires_at >= now()
                    """,
                    (lease_seconds, tenant_id, event_id, worker_id),
                )
                return cursor.rowcount == 1

    async def complete_outbox(self, tenant_id: str, event_id: str, *, worker_id: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE outbox_events
                    SET status = 'delivered', delivered_at = now(), last_error_code = NULL,
                        worker_id = NULL, lease_expires_at = NULL
                    WHERE tenant_id = %s AND event_id = %s AND status = 'processing'
                      AND worker_id = %s AND lease_expires_at >= now()
                    """,
                    (tenant_id, event_id, worker_id),
                )
                return cursor.rowcount == 1

    async def fail_outbox(
        self,
        tenant_id: str,
        event_id: str,
        *,
        worker_id: str,
        error_code: str,
        retry_at: datetime | None,
    ) -> bool:
        target_status = "pending" if retry_at is not None else "dead"
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE outbox_events
                    SET status = %s, available_at = COALESCE(%s, available_at),
                        claimed_at = NULL, worker_id = NULL, lease_expires_at = NULL,
                        last_error_code = %s
                    WHERE tenant_id = %s AND event_id = %s AND status = 'processing'
                      AND worker_id = %s AND lease_expires_at >= now()
                    """,
                    (target_status, retry_at, error_code, tenant_id, event_id, worker_id),
                )
                return cursor.rowcount == 1

    async def list_dead_outbox(self, *, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM outbox_events WHERE tenant_id = %s AND status = 'dead'
                    ORDER BY created_at LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                return list(await cursor.fetchall())

    async def replay_dead_outbox(self, tenant_id: str, event_id: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE outbox_events
                    SET status = 'pending', available_at = now(), claimed_at = NULL,
                        worker_id = NULL, lease_expires_at = NULL, last_error_code = NULL
                    WHERE tenant_id = %s AND event_id = %s AND status = 'dead'
                    """,
                    (tenant_id, event_id),
                )
                return cursor.rowcount == 1

    async def get_ticket_overview(self, tenant_id: str, ticket_id: str) -> dict[str, Any]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT * FROM ticket_sla WHERE tenant_id = %s AND ticket_id = %s",
                    (tenant_id, ticket_id),
                )
                sla = await cursor.fetchone()
                await cursor.execute(
                    "SELECT * FROM satisfaction_surveys WHERE tenant_id = %s AND ticket_id = %s",
                    (tenant_id, ticket_id),
                )
                survey = await cursor.fetchone()
                await cursor.execute(
                    """
                    SELECT message_id, direction, actor_type, actor_id, channel, content, created_at
                    FROM ticket_messages WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY created_at, message_id LIMIT 200
                    """,
                    (tenant_id, ticket_id),
                )
                messages = list(await cursor.fetchall())
                await cursor.execute(
                    """
                    SELECT assignment_id, team_id, member_id, reason_codes, assigned_at, ended_at
                    FROM ticket_assignments WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY assignment_id
                    """,
                    (tenant_id, ticket_id),
                )
                assignments = list(await cursor.fetchall())
                # RAG 建议引用：从最近一次已提交工作流意图中取 compose_answer 的 citations。
                await cursor.execute(
                    """
                    SELECT intent FROM ticket_workflow_runs
                    WHERE tenant_id = %s AND ticket_id = %s AND status = 'committed'
                    ORDER BY committed_at DESC NULLS LAST, created_at DESC
                    LIMIT 1
                    """,
                    (tenant_id, ticket_id),
                )
                run_row = await cursor.fetchone()
        citations: list[dict[str, Any]] = []
        if run_row and isinstance(run_row["intent"], dict):
            result = run_row["intent"].get("result") or {}
            citations = list(result.get("citations") or [])
        return {"sla": sla, "survey": survey, "messages": messages, "assignments": assignments, "citations": citations}

    async def ensure_sla_for_ticket(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        channel: str | None = None,
        category: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        reference = now or datetime.now(timezone.utc)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                policy = await self._resolve_sla_policy(cursor, tenant_id, category)
                if policy is None:
                    return False
                calendar = BusinessCalendar(
                    timezone_name=policy["timezone"],
                    business_days=frozenset(policy["business_days"]),
                    work_start=policy["work_start"],
                    work_end=policy["work_end"],
                )
                first_due = calendar.add_business_minutes(reference, policy["first_response_minutes"])
                resolution_due = calendar.add_business_minutes(reference, policy["resolution_minutes"])
                await cursor.execute(
                    """
                    INSERT INTO ticket_sla (
                        tenant_id, ticket_id, policy_id, policy_version,
                        first_response_due_at, resolution_due_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, ticket_id) DO NOTHING
                    """,
                    (tenant_id, ticket_id, policy["policy_id"], policy["version"], first_due, resolution_due),
                )
                return cursor.rowcount == 1

    async def _resolve_sla_policy(self, cursor, tenant_id: str, category: str | None) -> dict | None:
        """按分类链解析 SLA 策略：子分类策略 -> 父分类策略 -> 租户默认 SLA。

        不再取租户第一条 SLA：it.vpn 命中 sla-vpn，无子分类策略回退 it 策略，
        it 策略也未配置（或引用的 SLA 停用/缺失）时使用租户默认 SLA。
        """
        for key in sla_policy_candidates(category):
            await cursor.execute(
                """
                SELECT policy_id FROM tenant_it_policies
                WHERE tenant_id = %s AND category = %s AND active
                """,
                (tenant_id, key),
            )
            row = await cursor.fetchone()
            policy_id = None if row is None else row["policy_id"]
            if not policy_id:
                continue
            await cursor.execute(
                """
                SELECT policy_id, version, timezone, business_days, work_start, work_end,
                       first_response_minutes, resolution_minutes
                FROM sla_policies
                WHERE tenant_id = %s AND policy_id = %s AND active
                """,
                (tenant_id, policy_id),
            )
            policy = await cursor.fetchone()
            if policy is not None:
                return policy
        await cursor.execute(
            """
            SELECT policy_id, version, timezone, business_days, work_start, work_end,
                   first_response_minutes, resolution_minutes
            FROM sla_policies
            WHERE tenant_id = %s AND active
            ORDER BY created_at, policy_id
            LIMIT 1
            """,
            (tenant_id,),
        )
        return await cursor.fetchone()

    async def create_sla(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        policy_id: str,
        policy_version: int,
        started_at: datetime,
        first_response_minutes: int,
        resolution_minutes: int,
        calendar: BusinessCalendar,
    ) -> None:
        first_due = calendar.add_business_minutes(started_at, first_response_minutes)
        resolution_due = calendar.add_business_minutes(started_at, resolution_minutes)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO ticket_sla (
                        tenant_id, ticket_id, policy_id, policy_version,
                        first_response_due_at, resolution_due_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, ticket_id) DO NOTHING
                    """,
                    (tenant_id, ticket_id, policy_id, policy_version, first_due, resolution_due),
                )
                if cursor.rowcount != 1:
                    raise OperationsConflict("工单 SLA 已存在")

    async def reset_sla_on_reassignment(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        reassigned_at: datetime,
        first_response_minutes: int,
        resolution_minutes: int,
        calendar: BusinessCalendar,
        reset_enabled: bool,
    ) -> bool:
        if not reset_enabled:
            return False
        first_due = calendar.add_business_minutes(reassigned_at, first_response_minutes)
        resolution_due = calendar.add_business_minutes(reassigned_at, resolution_minutes)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_sla
                    SET first_response_due_at = %s,
                        resolution_due_at = %s,
                        paused_at = NULL,
                        pause_reason = NULL,
                        total_paused_seconds = 0,
                        first_responded_at = NULL,
                        first_response_breached_at = NULL,
                        resolution_breached_at = NULL,
                        updated_at = now()
                    WHERE tenant_id = %s AND ticket_id = %s
                    """,
                    (first_due, resolution_due, tenant_id, ticket_id),
                )
                return cursor.rowcount == 1

    async def mark_first_response(self, tenant_id: str, ticket_id: str, *, at: datetime | None = None) -> bool:
        reference = at or datetime.now(timezone.utc)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_sla
                    SET first_responded_at = %s, updated_at = now()
                    WHERE tenant_id = %s AND ticket_id = %s AND first_responded_at IS NULL
                    """,
                    (reference, tenant_id, ticket_id),
                )
                return cursor.rowcount == 1

    async def pause_sla(self, tenant_id: str, ticket_id: str, *, reason: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_sla SET paused_at = now(), pause_reason = %s, updated_at = now()
                    WHERE tenant_id = %s AND ticket_id = %s AND paused_at IS NULL
                    """,
                    (reason, tenant_id, ticket_id),
                )
                return cursor.rowcount == 1

    async def resume_sla(self, tenant_id: str, ticket_id: str, *, resumed_at: datetime) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_sla
                    SET first_response_due_at = first_response_due_at + (%s - paused_at),
                        resolution_due_at = resolution_due_at + (%s - paused_at),
                        total_paused_seconds = total_paused_seconds + EXTRACT(EPOCH FROM (%s - paused_at))::BIGINT,
                        paused_at = NULL, pause_reason = NULL, updated_at = now()
                    WHERE tenant_id = %s AND ticket_id = %s
                      AND paused_at IS NOT NULL AND %s >= paused_at
                    """,
                    (resumed_at, resumed_at, resumed_at, tenant_id, ticket_id, resumed_at),
                )
                return cursor.rowcount == 1

    async def scan_sla_breaches(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> int:
        if limit < 1 or limit > 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        reference = now or datetime.now(timezone.utc)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT tenant_id, ticket_id,
                           first_responded_at IS NULL
                               AND first_response_breached_at IS NULL
                               AND first_response_due_at <= %s AS first_breach,
                           resolution_breached_at IS NULL
                               AND resolution_due_at <= %s AS resolution_breach
                    FROM ticket_sla
                    WHERE paused_at IS NULL
                      AND (%s::TEXT IS NULL OR tenant_id = %s)
                      AND (
                          (first_responded_at IS NULL AND first_response_breached_at IS NULL
                           AND first_response_due_at <= %s)
                          OR (resolution_breached_at IS NULL AND resolution_due_at <= %s)
                      )
                    ORDER BY LEAST(first_response_due_at, resolution_due_at)
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (reference, reference, tenant_id, tenant_id, reference, reference, limit),
                )
                breaches = list(await cursor.fetchall())
                created = 0
                for breach in breaches:
                    kinds = []
                    if breach["first_breach"]:
                        kinds.append("first_response")
                    if breach["resolution_breach"]:
                        kinds.append("resolution")
                    await cursor.execute(
                        """
                        UPDATE ticket_sla
                        SET first_response_breached_at = CASE
                                WHEN %s THEN %s ELSE first_response_breached_at END,
                            resolution_breached_at = CASE
                                WHEN %s THEN %s ELSE resolution_breached_at END,
                            updated_at = now()
                        WHERE tenant_id = %s AND ticket_id = %s
                        """,
                        (
                            breach["first_breach"],
                            reference,
                            breach["resolution_breach"],
                            reference,
                            breach["tenant_id"],
                            breach["ticket_id"],
                        ),
                    )
                    for kind in kinds:
                        event_id = f"sla-{kind}-{breach['ticket_id']}"
                        idempotency_key = f"sla:{breach['ticket_id']}:{kind}"
                        await cursor.execute(
                            """
                            INSERT INTO outbox_events (
                                tenant_id, event_id, idempotency_key, event_type,
                                aggregate_type, aggregate_id, payload
                            ) VALUES (%s, %s, %s, 'sla.breached', 'ticket', %s, %s)
                            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                            """,
                            (
                                breach["tenant_id"],
                                event_id,
                                idempotency_key,
                                breach["ticket_id"],
                                Jsonb({"ticket_id": breach["ticket_id"], "kind": kind}),
                            ),
                        )
                        created += cursor.rowcount
                return created

    async def create_survey(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        survey_id: str,
        expires_at: datetime,
        outbox_event_id: str,
    ) -> bool:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO satisfaction_surveys (tenant_id, ticket_id, survey_id, expires_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, ticket_id) DO NOTHING
                    RETURNING survey_id
                    """,
                    (tenant_id, ticket_id, survey_id, expires_at),
                )
                if await cursor.fetchone() is None:
                    return False
                await cursor.execute(
                    """
                    INSERT INTO outbox_events (
                        tenant_id, event_id, idempotency_key, event_type,
                        aggregate_type, aggregate_id, payload
                    ) VALUES (%s, %s, %s, 'survey.send', 'ticket', %s, %s)
                    """,
                    (
                        tenant_id,
                        outbox_event_id,
                        f"survey:{ticket_id}",
                        ticket_id,
                        Jsonb({"ticket_id": ticket_id, "survey_id": survey_id}),
                    ),
                )
                return True

    async def respond_survey(
        self,
        *,
        tenant_id: str,
        survey_id: str,
        score: int,
        feedback: str | None = None,
    ) -> bool:
        if score < 1 or score > 5:
            raise ValueError("满意度评分必须在 1 到 5 之间")
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE satisfaction_surveys
                    SET status = 'responded', score = %s, feedback = %s, responded_at = now()
                    WHERE tenant_id = %s AND survey_id = %s
                      AND status IN ('pending', 'sent') AND expires_at > now()
                    """,
                    (score, feedback, tenant_id, survey_id),
                )
                return cursor.rowcount == 1

    async def expire_surveys(self, *, now: datetime | None = None) -> int:
        reference = now or datetime.now(timezone.utc)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE satisfaction_surveys SET status = 'expired'
                    WHERE status IN ('pending', 'sent') AND expires_at <= %s
                    """,
                    (reference,),
                )
                return cursor.rowcount
