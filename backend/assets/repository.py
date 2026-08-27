"""Tenant-scoped IT asset repository."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .models import AssetRecord, AssetStatus, CreateAsset, UpdateAsset


class AssetAlreadyExists(RuntimeError):
    pass


class AssetNotFound(LookupError):
    pass


class AssetRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, conninfo: str) -> "AssetRepository":
        pool = AsyncConnectionPool(conninfo, min_size=1, max_size=4, open=False, name="helpdesk-assets")
        await pool.open(wait=True)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def create(self, tenant_id: str, request: CreateAsset) -> AssetRecord:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                try:
                    await cursor.execute(
                        """
                        INSERT INTO it_assets (
                            tenant_id, asset_id, asset_no, asset_type, name, hostname,
                            ip_address, department, owner_user_id, uuid, serial, status,
                            purchased_at, warranty_expires_at, location, custom_fields
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (
                            tenant_id,
                            request.asset_id,
                            request.asset_no,
                            request.asset_type,
                            request.name,
                            request.hostname,
                            request.ip_address,
                            request.department,
                            request.owner_user_id,
                            request.uuid,
                            request.serial,
                            request.status.value,
                            request.purchased_at,
                            request.warranty_expires_at,
                            request.location,
                            Jsonb(request.custom_fields),
                        ),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise AssetAlreadyExists("资产或资产编号已存在") from exc
                row = await cursor.fetchone()
                if row is None:
                    raise AssetAlreadyExists("资产或资产编号已存在")
        return AssetRecord.model_validate(row)

    async def get(self, tenant_id: str, asset_id: str) -> AssetRecord | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM it_assets
                    WHERE tenant_id = %s AND asset_id = %s AND is_deleted = FALSE
                    """,
                    (tenant_id, asset_id),
                )
                row = await cursor.fetchone()
        return None if row is None else AssetRecord.model_validate(row)

    async def list_assets(
        self,
        tenant_id: str,
        *,
        owner_user_id: str | None = None,
        department: str | None = None,
        asset_type: str | None = None,
        status: AssetStatus | None = None,
        limit: int = 100,
    ) -> list[AssetRecord]:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        clauses = ["tenant_id = %s", "is_deleted = FALSE"]
        params: list = [tenant_id]
        if owner_user_id is not None:
            clauses.append("owner_user_id = %s")
            params.append(owner_user_id)
        if department is not None:
            clauses.append("department = %s")
            params.append(department)
        if asset_type is not None:
            clauses.append("asset_type = %s")
            params.append(asset_type)
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        params.append(limit)
        query = (
            "SELECT * FROM it_assets WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT %s"
        )
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(query, params)
                rows = await cursor.fetchall()
        return [AssetRecord.model_validate(row) for row in rows]

    async def update(self, tenant_id: str, asset_id: str, changes: UpdateAsset) -> AssetRecord:
        # exclude_unset 保留「显式置 null」：PATCH 传 {"hostname": null} 能清空字段，
        # 未提供的字段不更新。之前用 exclude_none 会把显式 null 一起丢掉，导致清空失效。
        data = changes.model_dump(exclude_unset=True)
        if not data:
            async with self.pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        "SELECT * FROM it_assets WHERE tenant_id = %s AND asset_id = %s AND is_deleted = FALSE",
                        (tenant_id, asset_id),
                    )
                    row = await cursor.fetchone()
            if row is None:
                raise AssetNotFound("资产不存在")
            return AssetRecord.model_validate(row)
        if "status" in data and data["status"] is not None:
            data["status"] = data["status"].value
        if "custom_fields" in data:
            data["custom_fields"] = Jsonb(data["custom_fields"] or {})
        assignments = ", ".join(f"{col} = %s" for col in data)
        values = list(data.values())
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                try:
                    await cursor.execute(
                        f"""
                        UPDATE it_assets
                        SET {assignments}, updated_at = now()
                        WHERE tenant_id = %s AND asset_id = %s AND is_deleted = FALSE
                        RETURNING *
                        """,
                        (*values, tenant_id, asset_id),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise AssetAlreadyExists("资产编号已被其他资产使用") from exc
                row = await cursor.fetchone()
        if row is None:
            raise AssetNotFound("资产不存在")
        return AssetRecord.model_validate(row)

    async def soft_delete(self, tenant_id: str, asset_id: str) -> bool:
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE it_assets SET is_deleted = TRUE, updated_at = now()
                    WHERE tenant_id = %s AND asset_id = %s AND is_deleted = FALSE
                    """,
                    (tenant_id, asset_id),
                )
                return cursor.rowcount == 1
