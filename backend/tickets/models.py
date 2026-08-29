"""Persistence-facing helpdesk models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.my_agent.helpdesk import ActorType, TicketStatus


class CreateTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    requester_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    channel: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    external_ticket_id: str | None = Field(default=None, min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=8_000)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    asset_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class TicketRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    ticket_id: str
    requester_id: str
    channel: str
    external_ticket_id: str | None
    title: str
    description: str
    status: TicketStatus
    priority: str
    category: str | None
    asset_id: str | None
    assigned_team_id: str | None
    assigned_user_id: str | None
    version: int
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None


class TicketStatusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: int
    tenant_id: str
    ticket_id: str
    from_status: TicketStatus | None
    to_status: TicketStatus
    action: str
    actor_type: ActorType
    actor_id: str
    ticket_version: int
    payload: dict[str, Any]
    occurred_at: datetime


class InboundEventResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    created: bool
    tenant_id: str
    channel: str
    external_event_id: str
    payload_hash: str
    ticket_id: str | None
    status: str = "received"
    attempts: int = 0
