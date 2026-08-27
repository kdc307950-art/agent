"""Tenant-scoped PostgreSQL repository for helpdesk tickets."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from src.my_agent.helpdesk import TicketCommand, TicketStatus, transition_ticket

from .models import CreateTicket, InboundEventResult, TicketRecord, TicketStatusEvent


class TicketAlreadyExists(RuntimeError):
    pass


class TicketNotFound(LookupError):
    pass


class TicketVersionConflict(RuntimeError):
    pass


class InboundEventConflict(RuntimeError):
    pass


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TicketRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @classmethod
    async def connect(
        cls,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
    ) -> "TicketRepository":
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
                await cursor.execute(
                    """
                    INSERT INTO tickets (
                        tenant_id, ticket_id, requester_id, channel,
                        external_ticket_id, title, description, status,
                        priority, version, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'new', %s, 0, %s)
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

    async def list_tickets(
        self,
        tenant_id: str,
        *,
        requester_id: str | None = None,
        statuses: tuple[TicketStatus, ...] = (),
        category: str | None = None,
        assigned_team_id: str | None = None,
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
    ) -> TicketRecord:
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
                    updated = await cursor.fetchone()
                    if updated is None:
                        raise TicketVersionConflict("工单版本已变化，请刷新后重试")
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

    async def register_inbound_event(
        self,
        tenant_id: str,
        channel: str,
        external_event_id: str,
        payload: dict[str, Any],
    ) -> InboundEventResult:
        payload_hash = canonical_payload_hash(payload)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO inbound_events (
                        tenant_id, channel, external_event_id, payload_hash
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, channel, external_event_id) DO NOTHING
                    RETURNING tenant_id, channel, external_event_id, payload_hash, ticket_id
                    """,
                    (tenant_id, channel, external_event_id, payload_hash),
                )
                row = await cursor.fetchone()
                created = row is not None
                if row is None:
                    await cursor.execute(
                        """
                        SELECT tenant_id, channel, external_event_id, payload_hash, ticket_id
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

    async def create_from_inbound_event(
        self,
        tenant_id: str,
        channel: str,
        external_event_id: str,
        event_payload: dict[str, Any],
        request: CreateTicket,
    ) -> tuple[bool, TicketRecord | None]:
        payload_hash = canonical_payload_hash(event_payload)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO inbound_events (
                        tenant_id, channel, external_event_id, payload_hash
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, channel, external_event_id) DO NOTHING
                    RETURNING external_event_id
                    """,
                    (tenant_id, channel, external_event_id, payload_hash),
                )
                if await cursor.fetchone() is None:
                    await cursor.execute(
                        """
                        SELECT payload_hash, ticket_id FROM inbound_events
                        WHERE tenant_id = %s AND channel = %s AND external_event_id = %s
                        """,
                        (tenant_id, channel, external_event_id),
                    )
                    existing = await cursor.fetchone()
                    if existing is None or existing["payload_hash"] != payload_hash:
                        raise InboundEventConflict("同一渠道事件标识对应了不同载荷")
                    ticket = None
                    if existing["ticket_id"] is not None:
                        await cursor.execute(
                            "SELECT * FROM tickets WHERE tenant_id = %s AND ticket_id = %s",
                            (tenant_id, existing["ticket_id"]),
                        )
                        row = await cursor.fetchone()
                        ticket = None if row is None else TicketRecord.model_validate(row)
                    return False, ticket

                await cursor.execute(
                    """
                    INSERT INTO tickets (
                        tenant_id, ticket_id, requester_id, channel,
                        external_ticket_id, title, description, status,
                        priority, version, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'new', %s, 0, %s)
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
                    ) VALUES (%s, %s, NULL, 'new', 'create', %s, %s, 0, %s)
                    """,
                    (
                        tenant_id,
                        request.ticket_id,
                        request.actor_type.value,
                        request.actor_id,
                        Jsonb(request.metadata),
                    ),
                )
                await cursor.execute(
                    """
                    UPDATE inbound_events SET ticket_id = %s, processed_at = now()
                    WHERE tenant_id = %s AND channel = %s AND external_event_id = %s
                    """,
                    (request.ticket_id, tenant_id, channel, external_event_id),
                )
                return True, TicketRecord.model_validate(row)

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
