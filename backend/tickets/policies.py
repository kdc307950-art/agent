"""Tenant IT service policies: per-category intake configuration.

每个 (tenant, category) 一条策略：必填字段、默认优先级、自动回答与人工审批开关。
category 支持点号子分类（it.vpn / it.account）。时间 SLA 不在此内联：
policy_id 引用 sla_policies（tenant_id, policy_id），由该表提供
first_response_minutes / resolution_minutes，避免两套 SLA 配置漂移。
"""

from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field


class TenantItPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    category: str
    policy_id: str | None
    required_fields: tuple[str, ...] = ()
    default_priority: str = "normal"
    auto_answer_enabled: bool = False
    approval_required: bool = False
    active: bool = True
    first_response_minutes: int | None = None
    resolution_minutes: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UpsertItPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=128)
    policy_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    required_fields: tuple[str, ...] = ()
    default_priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")
    auto_answer_enabled: bool = False
    approval_required: bool = False
    active: bool = True


def _row_to_policy(row: dict) -> TenantItPolicy:
    return TenantItPolicy(
        tenant_id=row["tenant_id"],
        category=row["category"],
        policy_id=row["policy_id"],
        required_fields=tuple(row["required_fields"]),
        default_priority=row["default_priority"],
        auto_answer_enabled=row["auto_answer_enabled"],
        approval_required=row["approval_required"],
        active=row["active"],
        first_response_minutes=row.get("first_response_minutes"),
        resolution_minutes=row.get("resolution_minutes"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ItPolicyRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def upsert(self, tenant_id: str, policy: UpsertItPolicy) -> TenantItPolicy:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                try:
                    await cursor.execute(
                        """
                        INSERT INTO tenant_it_policies (
                            tenant_id, category, policy_id, required_fields,
                            default_priority, auto_answer_enabled,
                            approval_required, active
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (tenant_id, category) DO UPDATE SET
                            policy_id = EXCLUDED.policy_id,
                            required_fields = EXCLUDED.required_fields,
                            default_priority = EXCLUDED.default_priority,
                            auto_answer_enabled = EXCLUDED.auto_answer_enabled,
                            approval_required = EXCLUDED.approval_required,
                            active = EXCLUDED.active,
                            updated_at = now()
                        RETURNING *
                        """,
                        (
                            tenant_id,
                            policy.category,
                            policy.policy_id,
                            list(policy.required_fields),
                            policy.default_priority,
                            policy.auto_answer_enabled,
                            policy.approval_required,
                            policy.active,
                        ),
                    )
                except psycopg.errors.ForeignKeyViolation as exc:
                    raise ItPolicyNotFound("引用的 SLA 策略不存在") from exc
                row = await cursor.fetchone()
        return _row_to_policy(row)

    async def get(self, tenant_id: str, category: str) -> TenantItPolicy | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT p.*, s.first_response_minutes, s.resolution_minutes
                    FROM tenant_it_policies AS p
                    LEFT JOIN sla_policies AS s
                      ON s.tenant_id = p.tenant_id AND s.policy_id = p.policy_id
                    WHERE p.tenant_id = %s AND p.category = %s
                    """,
                    (tenant_id, category),
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_policy(row)

    async def list_active(self, tenant_id: str) -> list[TenantItPolicy]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT p.*, s.first_response_minutes, s.resolution_minutes
                    FROM tenant_it_policies AS p
                    LEFT JOIN sla_policies AS s
                      ON s.tenant_id = p.tenant_id AND s.policy_id = p.policy_id
                    WHERE p.tenant_id = %s AND p.active
                    ORDER BY p.category
                    """,
                    (tenant_id,),
                )
                rows = await cursor.fetchall()
        return [_row_to_policy(row) for row in rows]

    async def delete(self, tenant_id: str, category: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM tenant_it_policies WHERE tenant_id = %s AND category = %s",
                    (tenant_id, category),
                )
                return cursor.rowcount == 1


class ItPolicyNotFound(LookupError):
    pass
