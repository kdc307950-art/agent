"""可信渠道身份目录 —— 渠道身份只在服务端登记，请求体不能伪造。

职责：
    - channel_identities 表： (tenant_id, channel, requester_id) 唯一
    - 保存可信 departments / asset_id / internal / 外部用户映射
    - 通用渠道端点、企业微信/钉钉 Webhook 受理时只从这里读取身份；
      无映射 -> 空部门 + 空资产 + 转人工

安全原则：
    - 请求体/事件 payload 中的 departments / asset_id 一律忽略
    - 映射只能由具备 security:admin 的管理端接口或专用 Webhook 验签后写入
    - asset_id 必须属于该租户且与请求人归属一致（由 API 层校验）
    - 保持与 copilot 身份快照一致：部门/内部标记是服务端事实，不信任前端
"""

from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field


class UpsertChannelIdentity(BaseModel):
    """服务端写入可信渠道身份的入参。

    tenant_id 来自认证主体/路径，不由请求体提交。
    """

    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    requester_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    departments: tuple[str, ...] = ()
    asset_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    internal: bool = False
    external_user_id: str | None = Field(default=None, max_length=256)
    mapping_source: str = Field(default="admin", min_length=1, max_length=32)
    active: bool = True


class ChannelIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    channel: str
    requester_id: str
    external_user_id: str | None = None
    departments: tuple[str, ...] = ()
    asset_id: str | None = None
    internal: bool = False
    mapping_source: str = "admin"
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _row_to_identity(row: dict) -> ChannelIdentity:
    return ChannelIdentity(
        tenant_id=row["tenant_id"],
        channel=row["channel"],
        requester_id=row["requester_id"],
        external_user_id=row.get("external_user_id"),
        departments=tuple(row.get("departments") or ()),
        asset_id=row.get("asset_id"),
        internal=bool(row.get("internal", False)),
        mapping_source=row.get("mapping_source") or "admin",
        active=bool(row.get("active", True)),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


class ChannelIdentityRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def upsert(self, tenant_id: str, identity: UpsertChannelIdentity) -> ChannelIdentity:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO channel_identities (
                        tenant_id, channel, requester_id, external_user_id,
                        departments, asset_id, internal, mapping_source, active
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, channel, requester_id) DO UPDATE SET
                        external_user_id = EXCLUDED.external_user_id,
                        departments = EXCLUDED.departments,
                        asset_id = EXCLUDED.asset_id,
                        internal = EXCLUDED.internal,
                        mapping_source = EXCLUDED.mapping_source,
                        active = EXCLUDED.active,
                        updated_at = now()
                    RETURNING *
                    """,
                    (
                        tenant_id,
                        identity.channel,
                        identity.requester_id,
                        identity.external_user_id,
                        list(identity.departments),
                        identity.asset_id,
                        identity.internal,
                        identity.mapping_source,
                        identity.active,
                    ),
                )
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("渠道身份写入后未返回行")
        return _row_to_identity(row)

    async def get(
        self, tenant_id: str, channel: str, requester_id: str
    ) -> ChannelIdentity | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM channel_identities
                    WHERE tenant_id = %s AND channel = %s AND requester_id = %s
                    """,
                    (tenant_id, channel, requester_id),
                )
                row = await cursor.fetchone()
        return _row_to_identity(row) if row else None

    async def list_admin(self, tenant_id: str, *, limit: int = 100) -> list[ChannelIdentity]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT * FROM channel_identities WHERE tenant_id = %s ORDER BY channel, requester_id LIMIT %s",
                    (tenant_id, limit),
                )
                rows = await cursor.fetchall()
        return [_row_to_identity(row) for row in rows]

    async def delete(self, tenant_id: str, channel: str, requester_id: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM channel_identities WHERE tenant_id = %s AND channel = %s AND requester_id = %s",
                    (tenant_id, channel, requester_id),
                )
                return cursor.rowcount == 1

