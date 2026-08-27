"""Operational routing rules and least-loaded on-duty member selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    team_id: str | None
    member_id: str | None
    reason_codes: tuple[str, ...]


class RoutingRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def route(
        self,
        *,
        tenant_id: str,
        category: str,
        subcategory: str | None,
        channel: str,
        department_id: str | None,
        risk_level: str,
        now: datetime | None = None,
    ) -> RoutingDecision:
        reference = now or datetime.now(timezone.utc)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM routing_rules
                    WHERE tenant_id = %s AND active
                      AND (category IS NULL OR category = %s)
                      AND (subcategory IS NULL OR subcategory = %s)
                      AND (channel IS NULL OR channel = %s)
                      AND (department_id IS NULL OR department_id = %s)
                    ORDER BY priority, rule_id
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (tenant_id, category, subcategory, channel, department_id),
                )
                rule = await cursor.fetchone()
                if rule is None:
                    return RoutingDecision(None, None, ("manual_queue_no_rule",))
                reasons = ["routing_rule"]
                if risk_level == "high":
                    reasons.insert(0, "high_risk_priority")
                await cursor.execute(
                    """
                    SELECT m.member_id,
                           (SELECT count(*) FROM tickets AS t
                            WHERE t.tenant_id = m.tenant_id
                              AND t.assigned_user_id = m.member_id
                              AND t.status IN ('assigned', 'in_progress')) AS current_load,
                           m.capacity
                    FROM support_members AS m
                    WHERE m.tenant_id = %s AND m.team_id = %s AND m.active
                      AND (%s::TEXT IS NULL OR %s = ANY(m.skills))
                      AND EXISTS (
                          SELECT 1 FROM support_schedules AS s
                          WHERE s.tenant_id = m.tenant_id AND s.member_id = m.member_id
                            AND s.starts_at <= %s AND s.ends_at > %s
                      )
                      AND (SELECT count(*) FROM tickets AS t
                           WHERE t.tenant_id = m.tenant_id
                             AND t.assigned_user_id = m.member_id
                             AND t.status IN ('assigned', 'in_progress')) < m.capacity
                    ORDER BY current_load, m.member_id
                    LIMIT 1
                    FOR UPDATE OF m SKIP LOCKED
                    """,
                    (
                        tenant_id,
                        rule["target_team_id"],
                        rule["required_skill"],
                        rule["required_skill"],
                        reference,
                        reference,
                    ),
                )
                member = await cursor.fetchone()
                if member is None:
                    return RoutingDecision(rule["target_team_id"], None, tuple((*reasons, "manual_queue_no_capacity")))
                return RoutingDecision(rule["target_team_id"], member["member_id"], tuple((*reasons, "least_loaded_on_duty")))
