"""工单周边运营数据：消息、Outbox 投递、SLA 实例与满意度调查。

职责：
    - Outbox 事务性发件箱：append_outbound_message 把「消息 + 出站事件」原子写入，
      由 outbox_worker 异步投递到渠道，保证业务与投递的一致性
    - SLA 实例：建单时按策略创建 ticket_sla（含业务日历），支持暂停/恢复/重派重置/
      违约扫描（scan_sla_breaches 会顺带写 sla.breached Outbox 事件）
    - 满意度调查：创建、过期、应答（1-5 分）
    - 工单概览聚合：SLA + 调查 + 消息 + 指派记录 + RAG 引用聚合返回

关键设计：
    - 所有「业务变更 + Outbox 事件」都在同一数据库事务中完成（原子性）
    - Outbox 领取用 FOR UPDATE SKIP LOCKED + 租约（lease）支持多 Worker 并发
    - 幂等：idempotency_key 唯一约束，重试不会重复投递
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .sla import BusinessCalendar


class OperationsConflict(RuntimeError):
    """运营数据冲突：如工单 SLA 已存在。"""


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
    """工单周边运营数据仓储：Outbox 发件箱、SLA 实例、满意度调查与工单概览聚合。

    所有写操作都与对应的 Outbox 事件在同一数据库事务内完成，保证「业务变更 + 渠道投递」
    的原子性；Outbox 领取使用 FOR UPDATE SKIP LOCKED + 租约，支持多 Worker 并发且不重复投递。
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @classmethod
    async def connect(
        cls,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
    ) -> TicketOperationsRepository:
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
        """追加工单出站消息，并在同一事务内写入对应 Outbox 事件。

        步骤：
          1. 先插 Outbox 事件（唯一键 tenant_id+idempotency_key 幂等；冲突则说明已投递，直接返回 False）；
          2. 再插 ticket_messages 消息记录；
        两者同事务提交，保证「消息落库 + 待投递事件」要么都发生、要么都不发生。
        """
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
                    # 幂等键冲突：该事件此前已登记，避免重复投递
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
        """领取一批待投递的 Outbox 事件（含失败重试恢复）。

        SQL 说明：
          - ready 子集：`pending` 且到重试时间，或 `processing` 但租约已过期（视为上一 Worker 失联，可被回收）；
          - `FOR UPDATE SKIP LOCKED`：锁住领到的事件但跳过已被其他 Worker 锁定的行，实现多副本并发领取；
          - 更新为 `processing` 并写租约（lease）与尝试次数；`lease_recovered` 标记是否回收了过期租约。
        """
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
                            OR (status = 'processing' AND (lease_expires_at IS NULL OR lease_expires_at < clock_timestamp()))
                        ) AND (%s::TEXT IS NULL OR tenant_id = %s)
                        ORDER BY available_at, created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE outbox_events AS o
                    SET status = 'processing', claimed_at = now(), attempts = attempts + 1,
                        worker_id = %s, lease_expires_at = clock_timestamp() + (%s * interval '1 second')
                    FROM ready
                    WHERE o.tenant_id = ready.tenant_id AND o.event_id = ready.event_id
                    RETURNING o.*, (ready.previous_status = 'processing') AS lease_recovered
                    """,
                    (tenant_id, tenant_id, limit, worker_id, lease_seconds),
                )
                return list(await cursor.fetchall())

    async def renew_outbox_lease(
        self, tenant_id: str, event_id: str, *, worker_id: str, lease_seconds: int = 60
    ) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE outbox_events
                    SET lease_expires_at = clock_timestamp() + (%s * interval '1 second')
                    WHERE tenant_id = %s AND event_id = %s AND status = 'processing'
                      AND worker_id = %s AND lease_expires_at >= clock_timestamp()
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
                      AND worker_id = %s AND lease_expires_at >= clock_timestamp()
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
                      AND worker_id = %s AND lease_expires_at >= clock_timestamp()
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
                        worker_id = NULL, lease_expires_at = NULL, last_error_code = NULL,
                        attempts = 0
                    WHERE tenant_id = %s AND event_id = %s AND status = 'dead'
                    """,
                    (tenant_id, event_id),
                )
                return cursor.rowcount == 1

    async def get_ticket_overview(self, tenant_id: str, ticket_id: str) -> dict[str, Any]:
        """聚合工单概览：SLA、满意度调查、消息流、指派记录与 RAG 建议引用。

        全部按 tenant_id + ticket_id 过滤（强制租户隔离），供详情页一次取全。
        """
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
        intake: dict[str, Any] = {}
        if run_row and isinstance(run_row["intent"], dict):
            result = run_row["intent"].get("result") or {}
            citations = list(result.get("citations") or [])
            intake = {
                "category": result.get("category"),
                "subcategory": result.get("subcategory"),
                "missing_fields": list(result.get("missing_fields") or []),
                "dispatch_reason_codes": list(result.get("dispatch_reason_codes") or []),
                "answer_status": result.get("answer_status"),
                "answer_reason_codes": list(result.get("answer_reason_codes") or []),
                "auto_reply": result.get("auto_reply"),
                "identity_missing": bool(result.get("identity_missing", False)),
                "risk_level": result.get("risk_level"),
            }
        handoff_reasons = sorted(
            set(intake.get("dispatch_reason_codes") or []) | set(intake.get("answer_reason_codes") or [])
        )
        return {
            "sla": sla,
            "survey": survey,
            "messages": messages,
            "assignments": assignments,
            "citations": citations,
            "intake": intake,
            "handoff_reasons": handoff_reasons,
        }

    async def ensure_sla_for_ticket(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        channel: str | None = None,
        category: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        reference = now or datetime.now(UTC)
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
                first_due = calendar.add_business_minutes(
                    reference, policy["first_response_minutes"]
                )
                resolution_due = calendar.add_business_minutes(
                    reference, policy["resolution_minutes"]
                )
                await cursor.execute(
                    """
                    INSERT INTO ticket_sla (
                        tenant_id, ticket_id, policy_id, policy_version,
                        first_response_due_at, resolution_due_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, ticket_id) DO NOTHING
                    """,
                    (
                        tenant_id,
                        ticket_id,
                        policy["policy_id"],
                        policy["version"],
                        first_due,
                        resolution_due,
                    ),
                )
                return cursor.rowcount == 1

    async def _resolve_sla_policy(
        self, cursor, tenant_id: str, category: str | None
    ) -> dict | None:
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

    async def mark_first_response(
        self, tenant_id: str, ticket_id: str, *, at: datetime | None = None
    ) -> bool:
        reference = at or datetime.now(UTC)
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
        """扫描已到期的 SLA：标记违约并写出 'sla.breached' Outbox 事件。

        - 只处理「未暂停」的 SLA（暂停期间不计时）；
        - 首次响应与解决分别计算：逾期且尚未标记违约的打入违约；
        - 违约标记与 Outbox 事件在同一事务内完成，且事件用幂等键防重复发送。
        返回本次新增的违约事件数。
        """
        if limit < 1 or limit > 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        reference = now or datetime.now(UTC)
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
        ticket_id: str | None = None,
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
                      AND (%s::TEXT IS NULL OR ticket_id = %s)
                      AND status IN ('pending', 'sent') AND expires_at > now()
                    """,
                    (score, feedback, tenant_id, survey_id, ticket_id, ticket_id),
                )
                return cursor.rowcount == 1

    async def expire_surveys(self, *, now: datetime | None = None) -> int:
        reference = now or datetime.now(UTC)
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
