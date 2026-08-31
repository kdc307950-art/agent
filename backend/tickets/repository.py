"""工单仓储层：租户隔离的 PostgreSQL 数据访问。

职责：
    - 工单 CRUD、乐观锁状态流转（transition_many）、状态流水
    - 建单/受理幂等：ON CONFLICT DO NOTHING + operation_id 去重
    - 渠道入站事件（inbound_events）与企微追问（pending_intake）登记/领取/重试
    - 工作流运行登记（ticket_workflow_runs）：operation_id 防重复提交，result_hash 幂等校验

关键设计：
    - 所有写操作在单连接事务内完成，行锁（FOR UPDATE）串行化并发修改
    - version 乐观锁：客户端必须携带 expected_version，冲突抛 TicketVersionConflict
    - SKIP LOCKED 领取机制：claim_* 方法支持多 Worker 副本并行而不重复处理
    - 异常类型细分（TicketNotFound / TicketVersionConflict / AssetBindingError /
      InboundEventConflict / WorkflowOperationConflict ...），上层据此映射 HTTP 状态码
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from src.my_agent.helpdesk import (
    ActorType,
    TicketAction,
    TicketCommand,
    TicketStatus,
    transition_ticket,
)

from .models import CreateTicket, InboundEventResult, TicketRecord, TicketStatusEvent


class TicketAlreadyExists(RuntimeError):
    """建单冲突：ticket_id 或渠道工单标识已存在。"""


class TicketNotFound(LookupError):
    """工单不存在（可能是租户不匹配或已删除）。"""


class TicketCapacityExceeded(RuntimeError):
    """指派失败：目标坐席不存在或负载已满。"""


class AssetBindingError(RuntimeError):
    """资产绑定失败：资产不存在、已删除或不属于当前用户/租户。"""


class TicketVersionConflict(RuntimeError):
    """乐观锁冲突：工单 version 已变化，调用方需刷新后重试。"""


class InboundEventConflict(RuntimeError):
    """渠道事件冲突：同一事件标识对应了不同载荷，或已关联其他工单。"""


class WorkflowOperationConflict(RuntimeError):
    """工作流操作冲突：operation_id 不存在、类型/版本不匹配或不可提交。"""


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """对载荷做稳定序列化后取 SHA-256。

    sort_keys + separators 保证相同语义的 dict 产生相同哈希，
    用于幂等校验：同一事件标识携带的载荷若哈希不一致则拒绝。
    """
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TicketRepository:
    """工单核心仓储：CRUD + 乐观锁流转 + 渠道/追问/工作流运行的登记与领取。

    所有写操作在单连接事务内完成，用行锁串行化并发修改；version 乐观锁由
    expected_version 校验，冲突抛 TicketVersionConflict。渠道入站与 Outbox
    使用 SKIP LOCKED 领取，支持多副本并行处理而不重复。
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
    ) -> TicketRepository:
        pool = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            open=False,
            name="helpdesk-tickets",
        )
        await pool.open(wait=True)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def create(self, tenant_id: str, request: CreateTicket) -> TicketRecord:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                # 客户只能将本人名下资产绑定到工单：校验与工单插入在同一事务内，
                # 并用 FOR UPDATE 锁定资产行直到提交，防止资产管理员在两步之间
                # 变更归属绕过校验（TOCTOU 并发窗口）。
                if request.asset_id is not None and request.actor_type == ActorType.CUSTOMER:
                    await cursor.execute(
                        """
                        SELECT owner_user_id FROM it_assets
                        WHERE tenant_id = %s AND asset_id = %s AND is_deleted = FALSE
                        FOR UPDATE
                        """,
                        (tenant_id, request.asset_id),
                    )
                    asset_row = await cursor.fetchone()
                    if (
                        asset_row is None
                        or asset_row["owner_user_id"] is None
                        or asset_row["owner_user_id"] != request.requester_id
                    ):
                        raise AssetBindingError("资产不存在或不属于当前用户")
                await cursor.execute(
                    """
                    INSERT INTO tickets (
                        tenant_id, ticket_id, requester_id, channel,
                        external_ticket_id, title, description, status,
                        priority, version, asset_id, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'new', %s, 0, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING *
                    """,
                    (
                        tenant_id,
                        request.ticket_id,
                        request.requester_id,
                        request.channel,
                        request.external_ticket_id,
                        request.title,
                        request.description,
                        request.priority,
                        request.asset_id,
                        Jsonb(request.metadata),
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise TicketAlreadyExists("工单或渠道工单标识已存在")
                await cursor.execute(
                    """
                    INSERT INTO ticket_status_events (
                        tenant_id, ticket_id, from_status, to_status, action,
                        actor_type, actor_id, ticket_version, payload
                    )
                    VALUES (%s, %s, NULL, 'new', 'create', %s, %s, 0, %s)
                    """,
                    (
                        tenant_id,
                        request.ticket_id,
                        request.actor_type.value,
                        request.actor_id,
                        Jsonb(request.metadata),
                    ),
                )
                return TicketRecord.model_validate(row)

    async def get(self, tenant_id: str, ticket_id: str) -> TicketRecord | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT * FROM tickets WHERE tenant_id = %s AND ticket_id = %s",
                    (tenant_id, ticket_id),
                )
                row = await cursor.fetchone()
        return None if row is None else TicketRecord.model_validate(row)

    async def bind_asset(self, tenant_id: str, ticket_id: str, asset_id: str) -> TicketRecord:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                # FK 只能保证租户和资产 ID 存在，不能阻止把软删除资产重新绑定。
                await cursor.execute(
                    """
                    SELECT is_deleted
                    FROM it_assets
                    WHERE tenant_id = %s AND asset_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, asset_id),
                )
                asset = await cursor.fetchone()
                if asset is None or bool(asset.get("is_deleted")):
                    raise AssetBindingError("资产不存在或不属于当前租户")
                try:
                    await cursor.execute(
                        """
                        UPDATE tickets SET asset_id = %s, updated_at = now()
                        WHERE tenant_id = %s AND ticket_id = %s
                        RETURNING *
                        """,
                        (asset_id, tenant_id, ticket_id),
                    )
                except psycopg.errors.ForeignKeyViolation as exc:
                    raise AssetBindingError("资产不存在或不属于当前租户") from exc
                row = await cursor.fetchone()
        if row is None:
            raise TicketNotFound("工单不存在")
        return TicketRecord.model_validate(row)

    async def unbind_asset(self, tenant_id: str, ticket_id: str) -> TicketRecord:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    UPDATE tickets SET asset_id = NULL, updated_at = now()
                    WHERE tenant_id = %s AND ticket_id = %s
                    RETURNING *
                    """,
                    (tenant_id, ticket_id),
                )
                row = await cursor.fetchone()
        if row is None:
            raise TicketNotFound("工单不存在")
        return TicketRecord.model_validate(row)

    async def list_tickets(
        self,
        tenant_id: str,
        *,
        requester_id: str | None = None,
        statuses: tuple[TicketStatus, ...] = (),
        category: str | None = None,
        assigned_team_id: str | None = None,
        assigned_user_id: str | None = None,
        asset_id: str | None = None,
        priority: str | None = None,
        query_text: str | None = None,
        updated_before: tuple[Any, str] | None = None,
        limit: int = 50,
    ) -> list[TicketRecord]:
        if limit < 1 or limit > 101:
            raise ValueError("limit 必须在 1 到 101 之间")
        clauses = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if requester_id is not None:
            clauses.append("requester_id = %s")
            params.append(requester_id)
        if statuses:
            clauses.append("status = ANY(%s)")
            params.append([status.value for status in statuses])
        if category is not None:
            clauses.append("category = %s")
            params.append(category)
        if assigned_team_id is not None:
            clauses.append("assigned_team_id = %s")
            params.append(assigned_team_id)
        if assigned_user_id is not None:
            clauses.append("assigned_user_id = %s")
            params.append(assigned_user_id)
        if asset_id is not None:
            clauses.append("asset_id = %s")
            params.append(asset_id)
        if priority is not None:
            clauses.append("priority = %s")
            params.append(priority)
        if query_text is not None:
            clauses.append("(title ILIKE %s OR description ILIKE %s)")
            pattern = f"%{query_text}%"
            params.extend((pattern, pattern))
        if updated_before is not None:
            clauses.append("(updated_at, ticket_id) < (%s, %s)")
            params.extend(updated_before)
        params.append(limit)
        query = (
            "SELECT * FROM tickets WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, ticket_id DESC LIMIT %s"
        )
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(query, params)
                rows = await cursor.fetchall()
        return [TicketRecord.model_validate(row) for row in rows]

    async def start_workflow_operation(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        operation_id: str,
        command_type: str,
        expected_version: int,
        checkpoint_thread_id: str,
    ) -> dict[str, Any]:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO ticket_workflow_runs (
                        tenant_id, ticket_id, operation_id, command_type,
                        expected_version, checkpoint_thread_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, ticket_id, operation_id) DO NOTHING
                    RETURNING *
                    """,
                    (
                        tenant_id,
                        ticket_id,
                        operation_id,
                        command_type,
                        expected_version,
                        checkpoint_thread_id,
                    ),
                )
                row = await cursor.fetchone()
                if row is not None:
                    return row
                await cursor.execute(
                    """
                    SELECT * FROM ticket_workflow_runs
                    WHERE tenant_id = %s AND ticket_id = %s AND operation_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, ticket_id, operation_id),
                )
                existing = await cursor.fetchone()
                if existing is None or existing["command_type"] != command_type:
                    raise WorkflowOperationConflict("operation_id 与已有工作流运行不匹配")
                if int(existing["expected_version"]) != expected_version:
                    raise WorkflowOperationConflict("operation_id 的工单版本不匹配")
                return existing

    async def get_workflow_operation(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        operation_id: str,
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM ticket_workflow_runs
                    WHERE tenant_id = %s AND ticket_id = %s AND operation_id = %s
                    """,
                    (tenant_id, ticket_id, operation_id),
                )
                return await cursor.fetchone()

    async def record_workflow_intent(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        operation_id: str,
        intent: dict[str, Any],
        checkpoint_id: str | None = None,
    ) -> None:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_workflow_runs
                    SET status = 'intent_recorded', intent = %s,
                        checkpoint_id = COALESCE(%s, checkpoint_id), updated_at = now()
                    WHERE tenant_id = %s AND ticket_id = %s AND operation_id = %s
                      AND status IN ('started', 'intent_recorded')
                    """,
                    (Jsonb(intent), checkpoint_id, tenant_id, ticket_id, operation_id),
                )
                if cursor.rowcount != 1:
                    raise WorkflowOperationConflict("工作流运行不可记录意图")

    async def mark_workflow_operation_failed(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        operation_id: str,
        error_code: str,
    ) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_workflow_runs
                    SET status = 'failed', error_code = %s, updated_at = now()
                    WHERE tenant_id = %s AND ticket_id = %s AND operation_id = %s
                      AND status IN ('started', 'intent_recorded')
                    """,
                    (error_code, tenant_id, ticket_id, operation_id),
                )
                return cursor.rowcount == 1

    async def list_recoverable_workflow_operations(
        self,
        *,
        older_than: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        reference = older_than or datetime.now(UTC)
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM ticket_workflow_runs
                    WHERE status IN ('started', 'intent_recorded') AND created_at <= %s
                    ORDER BY created_at
                    LIMIT %s
                    """,
                    (reference, limit),
                )
                return list(await cursor.fetchall())

    async def transition(
        self,
        tenant_id: str,
        command: TicketCommand,
        *,
        scopes: Iterable[str],
    ) -> TicketRecord:
        return await self.transition_many(tenant_id, [command], scopes=scopes)

    async def transition_many(
        self,
        tenant_id: str,
        commands: list[TicketCommand],
        *,
        scopes: Iterable[str],
        operation_id: str | None = None,
    ) -> TicketRecord:
        """在单个事务内按顺序执行一批状态流转命令（乐观锁 + 工作流运行幂等）。

        前置校验：
          - 所有命令必须作用于同一工单，且 expected_version 从起始版本起连续递增；
          - 工单行被 FOR UPDATE 锁定，并发写被串行化；
          - 若带 operation_id，先校验对应 ticket_workflow_runs：已 committed 则幂等返回当前快照，
            版本不匹配则抛 WorkflowOperationConflict；
          - 最终校验 current.version == expected_version，不一致抛 TicketVersionConflict。
        随后逐条执行 transition_ticket，并把多条 UPDATE 的聚合结果作为最终 TicketRecord 返回。
        """
        if not commands:
            raise ValueError("至少需要一个工单动作")
        ticket_id = commands[0].ticket_id
        if any(command.ticket_id != ticket_id for command in commands):
            raise ValueError("批量状态转换只能操作同一工单")
        expected_versions = [commands[0].expected_version + index for index in range(len(commands))]
        if [command.expected_version for command in commands] != expected_versions:
            raise ValueError("批量状态转换的 expected_version 必须连续")
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM tickets
                    WHERE tenant_id = %s AND ticket_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, ticket_id),
                )
                current = await cursor.fetchone()
                if current is None:
                    raise TicketNotFound("工单不存在")
                if operation_id is not None:
                    await cursor.execute(
                        """
                        SELECT status, expected_version FROM ticket_workflow_runs
                        WHERE tenant_id = %s AND ticket_id = %s AND operation_id = %s
                        FOR UPDATE
                        """,
                        (tenant_id, ticket_id, operation_id),
                    )
                    workflow_run = await cursor.fetchone()
                    if workflow_run is None:
                        raise WorkflowOperationConflict("未登记的 operation_id")
                    if workflow_run["status"] == "committed":
                        return TicketRecord.model_validate(current)
                    if int(workflow_run["expected_version"]) != commands[0].expected_version:
                        raise WorkflowOperationConflict("operation_id 的工单版本不匹配")
                if int(current["version"]) != commands[0].expected_version:
                    raise TicketVersionConflict("工单版本已变化，请刷新后重试")

                updated = current
                for command in commands:
                    source = TicketStatus(updated["status"])
                    target = transition_ticket(source, command, scopes=scopes)
                    next_version = command.expected_version + 1
                    await cursor.execute(
                        """
                        UPDATE tickets
                        SET status = %s,
                            version = %s,
                            updated_at = now(),
                            resolved_at = CASE
                                WHEN %s = 'resolved' THEN now()
                                WHEN %s = 'in_progress' AND status = 'resolved' THEN NULL
                                ELSE resolved_at
                            END,
                            closed_at = CASE WHEN %s = 'closed' THEN now() ELSE closed_at END,
                            category = CASE
                                WHEN %s = 'classify' THEN %s ELSE category END,
                            assigned_team_id = CASE
                                WHEN %s IN ('queue', 'assign') AND %s::TEXT IS NOT NULL THEN %s
                                ELSE assigned_team_id END,
                            assigned_user_id = CASE
                                WHEN %s = 'assign' AND %s::TEXT IS NOT NULL THEN %s
                                ELSE assigned_user_id END,
                            priority = CASE
                                WHEN %s::TEXT IS NOT NULL THEN %s ELSE priority END
                        WHERE tenant_id = %s AND ticket_id = %s AND version = %s
                        RETURNING *
                        """,
                        (
                            target.value,
                            next_version,
                            target.value,
                            target.value,
                            target.value,
                            command.action.value,
                            command.payload.get("category"),
                            command.action.value,
                            command.payload.get("team_id"),
                            command.payload.get("team_id"),
                            command.action.value,
                            command.payload.get("user_id"),
                            command.payload.get("user_id"),
                            command.payload.get("priority"),
                            command.payload.get("priority"),
                            tenant_id,
                            ticket_id,
                            command.expected_version,
                        ),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise TicketVersionConflict("工单版本已变化，请刷新后重试")
                    updated = row
                    if command.action == TicketAction.ASSIGN:
                        member_id = command.payload.get("user_id")
                        if member_id is not None:
                            await cursor.execute(
                                """
                                SELECT m.capacity,
                                       (SELECT count(*) FROM tickets AS t
                                        WHERE t.tenant_id = m.tenant_id
                                          AND t.assigned_user_id = m.member_id
                                          AND t.status IN ('assigned', 'in_progress')) AS current_load
                                FROM support_members AS m
                                WHERE m.tenant_id = %s AND m.member_id = %s
                                FOR UPDATE
                                """,
                                (tenant_id, member_id),
                            )
                            member_row = await cursor.fetchone()
                            if member_row is None:
                                raise TicketCapacityExceeded("指派成员不存在")
                            if int(member_row["current_load"]) >= int(member_row["capacity"]):
                                raise TicketCapacityExceeded("指派成员容量已满")
                        await cursor.execute(
                            """
                            UPDATE ticket_assignments SET ended_at = now()
                            WHERE tenant_id = %s AND ticket_id = %s AND ended_at IS NULL
                            """,
                            (tenant_id, ticket_id),
                        )
                        await cursor.execute(
                            """
                            INSERT INTO ticket_assignments (
                                tenant_id, ticket_id, team_id, member_id, reason_codes
                            )
                            SELECT %s, %s, team_id, %s, %s
                            FROM support_teams
                            WHERE tenant_id = %s AND team_id = %s
                              AND (%s::TEXT IS NULL OR EXISTS (
                                  SELECT 1 FROM support_members
                                  WHERE tenant_id = %s AND member_id = %s AND team_id = %s
                              ))
                            """,
                            (
                                tenant_id,
                                ticket_id,
                                command.payload.get("user_id"),
                                list(command.payload.get("reason_codes") or []),
                                tenant_id,
                                command.payload.get("team_id"),
                                command.payload.get("user_id"),
                                tenant_id,
                                command.payload.get("user_id"),
                                command.payload.get("team_id"),
                            ),
                        )
                    await cursor.execute(
                        """
                        INSERT INTO ticket_status_events (
                            tenant_id, ticket_id, from_status, to_status, action,
                            actor_type, actor_id, ticket_version, payload
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            tenant_id,
                            ticket_id,
                            source.value,
                            target.value,
                            command.action.value,
                            command.actor_type.value,
                            command.actor_id,
                            next_version,
                            Jsonb(command.payload),
                        ),
                    )
                if operation_id is not None:
                    result_hash = canonical_payload_hash(
                        {
                            "ticket_id": ticket_id,
                            "version": int(updated["version"]),
                            "status": updated["status"],
                        }
                    )
                    await cursor.execute(
                        """
                        UPDATE ticket_workflow_runs
                        SET status = 'committed', result_hash = %s,
                            committed_at = now(), updated_at = now()
                        WHERE tenant_id = %s AND ticket_id = %s AND operation_id = %s
                          AND status IN ('started', 'intent_recorded')
                        """,
                        (result_hash, tenant_id, ticket_id, operation_id),
                    )
                    if cursor.rowcount != 1:
                        raise WorkflowOperationConflict("工作流运行不可提交")
                return TicketRecord.model_validate(updated)

    async def list_status_events(
        self,
        tenant_id: str,
        ticket_id: str,
    ) -> list[TicketStatusEvent]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT event_id, tenant_id, ticket_id, from_status, to_status,
                           action, actor_type, actor_id, ticket_version, payload,
                           occurred_at
                    FROM ticket_status_events
                    WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY event_id
                    """,
                    (tenant_id, ticket_id),
                )
                rows = await cursor.fetchall()
        return [TicketStatusEvent.model_validate(row) for row in rows]

    async def append_status_event(
        self,
        tenant_id: str,
        ticket_id: str,
        *,
        action: str,
        actor_type: str,
        actor_id: str,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        """追加一条状态流水（不改变工单状态）。

        ``dedupe_key`` 用于渠道重试等至少一次投递场景；键写入 payload
        并在插入前检查，避免同一外部事件重复追加中间流水。
        """
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                event_payload = dict(payload or {})
                if dedupe_key:
                    event_payload["_dedupe_key"] = dedupe_key[:256]
                if dedupe_key:
                    # 锁住父工单后再检查去重键；同一事件的重试即使由两个
                    # Worker 并行执行，也只能有一个事务通过检查并插入。
                    await cursor.execute(
                        """
                        SELECT 1 FROM tickets
                        WHERE tenant_id = %s AND ticket_id = %s
                        FOR UPDATE
                        """,
                        (tenant_id, ticket_id),
                    )
                    if await cursor.fetchone() is None:
                        return False
                    await cursor.execute(
                        """
                        INSERT INTO ticket_status_events (
                            tenant_id, ticket_id, from_status, to_status, action,
                            actor_type, actor_id, ticket_version, payload
                        )
                        SELECT t.tenant_id, t.ticket_id, t.status, t.status, %s, %s, %s,
                               t.version, %s
                        FROM tickets AS t
                        WHERE t.tenant_id = %s AND t.ticket_id = %s
                          AND NOT EXISTS (
                              SELECT 1 FROM ticket_status_events AS e
                              WHERE e.tenant_id = %s AND e.ticket_id = %s
                                AND e.payload->>'_dedupe_key' = %s
                          )
                        """,
                        (
                            action,
                            actor_type,
                            actor_id,
                            Jsonb(event_payload),
                            tenant_id,
                            ticket_id,
                            tenant_id,
                            ticket_id,
                            dedupe_key[:256],
                        ),
                    )
                    return cursor.rowcount == 1
                await cursor.execute(
                    """
                    INSERT INTO ticket_status_events (
                        tenant_id, ticket_id, from_status, to_status, action,
                        actor_type, actor_id, ticket_version, payload
                    )
                    SELECT t.tenant_id, t.ticket_id, t.status, t.status, %s, %s, %s,
                           t.version,
                           %s
                    FROM tickets AS t
                    WHERE t.tenant_id = %s AND t.ticket_id = %s
                    """,
                    (action, actor_type, actor_id, Jsonb(event_payload), tenant_id, ticket_id),
                )
                return cursor.rowcount == 1

    # ========== 企微追问 Resume：客户待补全关联 ==========

    async def register_pending_intake(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        channel: str,
        external_user_id: str,
        required_fields: list[str],
        expires_at,
    ) -> None:
        """登记/更新客户待补全记录；同一客户的其他 awaiting 记录自动取消（保持唯一）。"""
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_customer_pending_intake
                    SET status = 'cancelled', updated_at = now()
                    WHERE tenant_id = %s AND channel = %s AND external_user_id = %s
                      AND status = 'awaiting' AND ticket_id <> %s
                    """,
                    (tenant_id, channel, external_user_id, ticket_id),
                )
                await cursor.execute(
                    """
                    INSERT INTO ticket_customer_pending_intake (
                        tenant_id, ticket_id, channel, external_user_id,
                        required_fields, status, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, 'awaiting', %s)
                    ON CONFLICT (tenant_id, ticket_id) DO UPDATE SET
                        channel = EXCLUDED.channel,
                        external_user_id = EXCLUDED.external_user_id,
                        required_fields = EXCLUDED.required_fields,
                        status = 'awaiting',
                        expires_at = EXCLUDED.expires_at,
                        updated_at = now()
                    """,
                    (tenant_id, ticket_id, channel, external_user_id, required_fields, expires_at),
                )

    async def find_pending_intake(
        self,
        tenant_id: str,
        channel: str,
        external_user_id: str,
    ) -> dict[str, Any] | None:
        """按租户/渠道/用户查唯一有效（awaiting 且未过期）的待补全记录。"""
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM ticket_customer_pending_intake
                    WHERE tenant_id = %s AND channel = %s AND external_user_id = %s
                      AND status = 'awaiting' AND expires_at > now()
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (tenant_id, channel, external_user_id),
                )
                return await cursor.fetchone()

    async def get_pending_intake(self, tenant_id: str, ticket_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM ticket_customer_pending_intake
                    WHERE tenant_id = %s AND ticket_id = %s
                    """,
                    (tenant_id, ticket_id),
                )
                return await cursor.fetchone()

    async def list_pending_intakes(
        self, tenant_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT tenant_id, ticket_id, channel, external_user_id,
                           required_fields, status, resume_count, expires_at, updated_at
                    FROM ticket_customer_pending_intake
                    WHERE tenant_id = %s
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                return list(await cursor.fetchall())

    async def mark_pending_intake_resumed(self, tenant_id: str, ticket_id: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_customer_pending_intake
                    SET status = 'resumed', resume_count = resume_count + 1, updated_at = now()
                    WHERE tenant_id = %s AND ticket_id = %s AND status = 'awaiting'
                    """,
                    (tenant_id, ticket_id),
                )
                return cursor.rowcount == 1

    async def expire_pending_intakes(self, *, now=None) -> int:
        reference = now or datetime.now(UTC)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ticket_customer_pending_intake
                    SET status = 'expired', updated_at = now()
                    WHERE status = 'awaiting' AND expires_at <= %s
                    """,
                    (reference,),
                )
                return cursor.rowcount

    async def register_inbound_event(
        self,
        tenant_id: str,
        channel: str,
        external_event_id: str,
        payload: dict[str, Any],
    ) -> InboundEventResult:
        """登记渠道入站事件（快速 ACK 阶段）——不建单，返回 received 状态。

        幂等：同 (tenant_id, channel, external_event_id) 只登记一次，
        重复调用返回已有记录（created=False），由 InboundWorker 异步建单受理。
        """
        payload_hash = canonical_payload_hash(payload)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO inbound_events (
                        tenant_id, channel, external_event_id, payload_hash,
                        payload, status, attempts, next_attempt_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'received', 0, now())
                    ON CONFLICT (tenant_id, channel, external_event_id) DO NOTHING
                    RETURNING tenant_id, channel, external_event_id, payload_hash, ticket_id, status, attempts
                    """,
                    (tenant_id, channel, external_event_id, payload_hash, Jsonb(payload)),
                )
                row = await cursor.fetchone()
                created = row is not None
                if row is None:
                    await cursor.execute(
                        """
                        SELECT tenant_id, channel, external_event_id, payload_hash, ticket_id, status, attempts
                        FROM inbound_events
                        WHERE tenant_id = %s AND channel = %s AND external_event_id = %s
                        FOR UPDATE
                        """,
                        (tenant_id, channel, external_event_id),
                    )
                    row = await cursor.fetchone()
                if row is None or row["payload_hash"] != payload_hash:
                    raise InboundEventConflict("同一渠道事件标识对应了不同载荷")
                return InboundEventResult(created=created, **row)

    async def get_inbound_event(
        self,
        tenant_id: str,
        channel: str,
        external_event_id: str,
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM inbound_events
                    WHERE tenant_id = %s AND channel = %s AND external_event_id = %s
                    """,
                    (tenant_id, channel, external_event_id),
                )
                return await cursor.fetchone()

    async def list_inbound_events(
        self,
        tenant_id: str,
        external_event_id: str,
    ) -> list[dict[str, Any]]:
        """按租户 + 事件 ID 列出全部渠道记录（状态查询 API 用）。"""
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT tenant_id, channel, external_event_id, status, ticket_id,
                           attempts, error_code, received_at, processed_at
                    FROM inbound_events
                    WHERE tenant_id = %s AND external_event_id = %s
                    ORDER BY received_at
                    """,
                    (tenant_id, external_event_id),
                )
                return list(await cursor.fetchall())

    async def claim_inbound_events(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
        limit: int = 20,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """领取待处理入站事件（received / 可重试 failed / 租约过期的 processing）。

        与 Outbox 同款 FOR UPDATE SKIP LOCKED，支持多 Worker 副本。
        """
        if not worker_id or lease_seconds < 1 or limit < 1 or limit > 100:
            raise ValueError("Inbound 领取参数无效")
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    WITH ready AS (
                        SELECT tenant_id, channel, external_event_id, status AS previous_status
                        FROM inbound_events
                        WHERE (
                            (status = 'received' AND next_attempt_at <= now())
                            OR (status = 'failed' AND next_attempt_at <= now())
                            OR (status = 'processing' AND (lease_expires_at IS NULL OR lease_expires_at < clock_timestamp()))
                        )
                          AND (%s::TEXT IS NULL OR tenant_id = %s)
                        ORDER BY next_attempt_at, received_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE inbound_events AS e
                    SET status = 'processing', claimed_at = now(),
                        attempts = attempts + 1, worker_id = %s,
                        lease_expires_at = clock_timestamp() + (%s * interval '1 second'),
                        error_code = NULL
                    FROM ready
                    WHERE e.tenant_id = ready.tenant_id AND e.channel = ready.channel
                      AND e.external_event_id = ready.external_event_id
                    RETURNING e.*, (ready.previous_status = 'processing') AS lease_recovered
                    """,
                    (tenant_id, tenant_id, limit, worker_id, lease_seconds),
                )
                return list(await cursor.fetchall())

    async def renew_inbound_lease(
        self,
        tenant_id: str,
        external_event_id: str,
        *,
        channel: str | None = None,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE inbound_events
                    SET lease_expires_at = clock_timestamp() + (%s * interval '1 second')
                    WHERE tenant_id = %s AND external_event_id = %s
                      AND (%s::TEXT IS NULL OR channel = %s)
                      AND status = 'processing' AND worker_id = %s
                      AND lease_expires_at >= clock_timestamp()
                    """,
                    (lease_seconds, tenant_id, external_event_id, channel, channel, worker_id),
                )
                return cursor.rowcount == 1

    async def complete_inbound_event(
        self,
        tenant_id: str,
        external_event_id: str,
        *,
        channel: str | None = None,
        ticket_id: str,
        worker_id: str,
    ) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE inbound_events
                    SET status = 'committed', ticket_id = %s, processed_at = now(),
                        worker_id = NULL, lease_expires_at = NULL, error_code = NULL
                    WHERE tenant_id = %s AND external_event_id = %s
                      AND (%s::TEXT IS NULL OR channel = %s)
                      AND status = 'processing' AND worker_id = %s
                      AND lease_expires_at >= clock_timestamp()
                    """,
                    (ticket_id, tenant_id, external_event_id, channel, channel, worker_id),
                )
                return cursor.rowcount == 1

    async def fail_inbound_event(
        self,
        tenant_id: str,
        external_event_id: str,
        *,
        channel: str | None = None,
        worker_id: str,
        error_code: str,
        retry_at: Any,
        max_attempts: int = 5,
    ) -> bool:
        target_status = "dead" if retry_at is None else "failed"
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE inbound_events
                    SET status = CASE WHEN %s::boolean THEN 'dead' ELSE 'failed' END,
                        next_attempt_at = COALESCE(%s, next_attempt_at),
                        claimed_at = NULL, worker_id = NULL, lease_expires_at = NULL,
                        error_code = %s
                    WHERE tenant_id = %s AND external_event_id = %s
                      AND (%s::TEXT IS NULL OR channel = %s)
                      AND status = 'processing' AND worker_id = %s
                      AND lease_expires_at >= clock_timestamp()
                    """,
                    (
                        target_status == "dead",
                        retry_at,
                        error_code,
                        tenant_id,
                        external_event_id,
                        channel,
                        channel,
                        worker_id,
                    ),
                )
                return cursor.rowcount == 1

    async def replay_inbound_event(
        self, tenant_id: str, external_event_id: str, *, channel: str | None = None
    ) -> bool:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                if channel is None:
                    await cursor.execute(
                        """
                        SELECT channel FROM inbound_events
                        WHERE tenant_id = %s AND external_event_id = %s AND status = 'dead'
                        FOR UPDATE
                        """,
                        (tenant_id, external_event_id),
                    )
                    rows = await cursor.fetchall()
                    # Without channel, replay is safe only when the event ID is
                    # unambiguous within the tenant.
                    if len(rows) != 1:
                        return False
                    channel = str(rows[0][0])
                await cursor.execute(
                    """
                    UPDATE inbound_events
                    SET status = 'received', next_attempt_at = now(), attempts = 0,
                        claimed_at = NULL, worker_id = NULL, lease_expires_at = NULL,
                        error_code = NULL
                    WHERE tenant_id = %s AND channel = %s AND external_event_id = %s AND status = 'dead'
                    """,
                    (tenant_id, channel, external_event_id),
                )
                return cursor.rowcount == 1

    async def attach_inbound_event(
        self,
        tenant_id: str,
        channel: str,
        external_event_id: str,
        ticket_id: str,
    ) -> None:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE inbound_events
                    SET ticket_id = %s, processed_at = now()
                    WHERE tenant_id = %s AND channel = %s AND external_event_id = %s
                      AND (ticket_id IS NULL OR ticket_id = %s)
                    """,
                    (ticket_id, tenant_id, channel, external_event_id, ticket_id),
                )
                if cursor.rowcount != 1:
                    raise InboundEventConflict("渠道事件不存在或已关联其他工单")


@asynccontextmanager
async def ticket_repository_context(conninfo: str) -> AsyncIterator[TicketRepository]:
    repository = await TicketRepository.connect(conninfo)
    try:
        yield repository
    finally:
        await repository.close()
