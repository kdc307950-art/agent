"""IT 资产持久化模型 —— 资产台账的 Pydantic 数据契约。

职责：
    - 定义资产的状态枚举（AssetStatus）与三类模型：
      创建请求 CreateAsset / 更新请求 UpdateAsset / 仓储读取结果 AssetRecord

关键设计：
    - extra="forbid"：拒绝未声明字段，防止 API 层静默吞掉拼写错误或越权字段
    - AssetRecord 为 frozen（不可变），作为只读快照从仓储安全返回
    - UpdateAsset 所有字段可选且可显式置 null，配合仓储层 exclude_unset
      实现「PATCH 传 null 即清空」的语义
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AssetStatus(StrEnum):
    """资产生命周期状态枚举（值即数据库存储字符串）。

    - in_stock：在库（尚未领用）
    - in_use：使用中（默认，新建资产即处于该状态）
    - repairing：维修中
    - retired：已报废
    """

    IN_STOCK = "in_stock"
    IN_USE = "in_use"
    REPAIRING = "repairing"
    RETIRED = "retired"


class CreateAsset(BaseModel):
    """创建资产的请求体（POST /assets）。

    asset_id 为业务主键，需匹配 ^[A-Za-z0-9_.-]+$ 且长度 1~64；
    asset_no（资产编号）与 asset_type（资产类型）为必填。
    custom_fields 用于承载各类型资产的自定义扩展字段，默认空字典。
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    asset_no: str = Field(min_length=1, max_length=128)
    asset_type: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=64)
    department: str | None = Field(default=None, max_length=128)
    owner_user_id: str | None = Field(default=None, max_length=128)
    uuid: str | None = Field(default=None, max_length=255)
    serial: str | None = Field(default=None, max_length=255)
    status: AssetStatus = AssetStatus.IN_USE
    purchased_at: datetime | None = None
    warranty_expires_at: datetime | None = None
    location: str | None = Field(default=None, max_length=255)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class UpdateAsset(BaseModel):
    """局部更新资产的请求体（PATCH /assets/{asset_id}）。

    所有字段均可选：仓储层用 model_dump(exclude_unset=True) 只更新
    显式提供的字段；显式传 null 表示清空该字段（如删除 hostname）。
    custom_fields 传 null 表示清空自定义字段。
    """

    model_config = ConfigDict(extra="forbid")

    asset_no: str | None = Field(default=None, max_length=128)
    asset_type: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=64)
    department: str | None = Field(default=None, max_length=128)
    owner_user_id: str | None = Field(default=None, max_length=128)
    uuid: str | None = Field(default=None, max_length=255)
    serial: str | None = Field(default=None, max_length=255)
    status: AssetStatus | None = None
    purchased_at: datetime | None = None
    warranty_expires_at: datetime | None = None
    location: str | None = Field(default=None, max_length=255)
    custom_fields: dict[str, Any] | None = None


class AssetRecord(BaseModel):
    """资产台账的完整记录（仓储层读取/写入返回的统一快照）。

    frozen=True：对象创建后不可变，避免上层误改已落库的数据；
    extra="forbid"：数据库新列若未在此声明，读取时会显式报错而不是静默丢弃。
    与 CreateAsset 相比额外包含 tenant_id（租户归属）、is_deleted（软删除标记）
    与 created_at / updated_at（时间戳）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    asset_id: str
    asset_no: str
    asset_type: str
    name: str | None
    hostname: str | None
    ip_address: str | None
    department: str | None
    owner_user_id: str | None
    uuid: str | None
    serial: str | None
    status: AssetStatus
    purchased_at: datetime | None
    warranty_expires_at: datetime | None
    location: str | None
    custom_fields: dict[str, Any]
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
