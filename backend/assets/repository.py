"""租户隔离的 IT 资产仓储层 —— 资产台账的 PostgreSQL 持久化。

职责：
    - 封装 it_assets 表的增删改查：create / get / list_assets / update / soft_delete
    - 定义仓储层专属异常：AssetAlreadyExists（唯一键冲突）、AssetNotFound（未找到）

关键设计：
    - 租户隔离：每条 SQL 都带 tenant_id 条件，杜绝跨租户读写
    - 软删除：delete 只置 is_deleted=TRUE，查询一律过滤 is_deleted=FALSE，
      保证台账可追溯、可恢复
    - 局部更新：update 用 exclude_unset 只更新显式字段，传 null 即清空；
      并发冲突由数据库唯一约束兜底并映射为 AssetAlreadyExists
    - 连接池：AsyncConnectionPool 由 connect() 建立、close() 关闭，
      池参数（min_size=1, max_size=4）为轻量台账访问设计
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .models import AssetRecord, AssetStatus, CreateAsset, UpdateAsset


class AssetAlreadyExists(RuntimeError):
    """资产 ID 或资产编号已存在（数据库唯一约束冲突），API 层映射为 409。"""

    pass


class AssetNotFound(LookupError):
    """资产不存在（或已被软删除），API 层映射为 404。"""

    pass


class AssetRepository:
    """基于 psycopg 异步连接池的资产台账仓储。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        # 注入连接池而非自行创建，便于测试时替换为模拟池。
        self.pool = pool

    @classmethod
    async def connect(cls, conninfo: str) -> AssetRepository:
        """按连接串建立专用连接池并返回仓储实例（同步等待池就绪）。"""
        pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=4, open=False, name="helpdesk-assets"
        )
        await pool.open(wait=True)
        return cls(pool)

    async def close(self) -> None:
        """关闭连接池，释放数据库连接（进程退出时调用）。"""
        await self.pool.close()

    async def create(self, tenant_id: str, request: CreateAsset) -> AssetRecord:
        """插入一条新资产并返回完整记录。

        参数：tenant_id 资产归属租户；request 创建请求体。
        返回：AssetRecord。
        抛错：AssetAlreadyExists —— 资产 ID 或资产编号与现有记录冲突。
        设计：custom_fields 以 Jsonb 类型落库；插入与冲突捕获在同一事务内。
        """
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
                    # 唯一约束（tenant_id+asset_id 或 asset_no）被触发，转成领域异常。
                    raise AssetAlreadyExists("资产或资产编号已存在") from exc
                row = await cursor.fetchone()
                if row is None:
                    # 理论上 INSERT RETURNING 不会返回空行，防御性兜底。
                    raise AssetAlreadyExists("资产或资产编号已存在")
        return AssetRecord.model_validate(row)

    async def get(self, tenant_id: str, asset_id: str) -> AssetRecord | None:
        """按租户 + 资产 ID 读取单个资产（未删除）；不存在返回 None。"""
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
        """按条件过滤资产列表，按 updated_at 倒序返回。

        参数：tenant_id 必填（租户隔离）；owner_user_id / department /
            asset_type / status 为可选过滤条件；limit 限制条数（1~500）。
        返回：AssetRecord 列表。
        设计：WHERE 子句按提供的条件动态拼接（参数化查询防注入），
        始终带 tenant_id 与 is_deleted=FALSE 两个恒定条件。
        """
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
        """局部更新资产，返回更新后的记录。

        参数：tenant_id 租户；asset_id 资产 ID；changes 更新请求体。
        返回：AssetRecord。
        抛错：AssetNotFound（不存在或已删除）；AssetAlreadyExists（编号冲突）。
        设计：exclude_unset 保留「显式置 null」：PATCH 传 {"hostname": null}
        能清空字段，未提供的字段不更新。之前用 exclude_none 会把显式
        null 一起丢掉，导致清空失效。
        """
        # exclude_unset 保留「显式置 null」：PATCH 传 {"hostname": null} 能清空字段，
        # 未提供的字段不更新。之前用 exclude_none 会把显式 null 一起丢掉，导致清空失效。
        data = changes.model_dump(exclude_unset=True)
        if not data:
            # 请求体没有任何字段：等价于「读一次并校验存在性」，返回现状即可。
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
            # 状态枚举需要转回字符串再写入数据库。
            data["status"] = data["status"].value
        if "custom_fields" in data:
            # 自定义字段以 Jsonb 类型落库；显式传空字典/None 视为清空。
            data["custom_fields"] = Jsonb(data["custom_fields"] or {})
        # 由字段名拼出 "col = %s" 占位符列表，值按同序绑定（参数化防注入）。
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
                    # 更新后 asset_no 与其它资产冲突（唯一索引）时映射为领域异常。
                    raise AssetAlreadyExists("资产编号已被其他资产使用") from exc
                row = await cursor.fetchone()
        if row is None:
            # WHERE 条件未命中：资产不存在或已被软删除。
            raise AssetNotFound("资产不存在")
        return AssetRecord.model_validate(row)

    async def soft_delete(self, tenant_id: str, asset_id: str) -> bool:
        """软删除资产：置 is_deleted=TRUE 并刷新 updated_at。

        返回：bool —— True 表示确实删除了一条；False 表示资产不存在/已删除。
        设计：不物理删除行，历史数据保留在表中供审计与恢复。
        """
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE it_assets SET is_deleted = TRUE, updated_at = now()
                    WHERE tenant_id = %s AND asset_id = %s AND is_deleted = FALSE
                    """,
                    (tenant_id, asset_id),
                )
                # rowcount 只有 0/1 两态：0 = 无匹配行，1 = 命中并置位。
                return cursor.rowcount == 1
