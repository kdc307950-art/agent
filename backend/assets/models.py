"""IT asset persistence models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AssetStatus(StrEnum):
    IN_STOCK = "in_stock"
    IN_USE = "in_use"
    REPAIRING = "repairing"
    RETIRED = "retired"


class CreateAsset(BaseModel):
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
