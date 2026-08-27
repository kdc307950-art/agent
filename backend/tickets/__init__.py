"""Helpdesk ticket persistence package."""

from .models import CreateTicket, InboundEventResult, TicketRecord, TicketStatusEvent
from .operations import OperationsConflict, TicketOperationsRepository
from .policies import ItPolicyNotFound, ItPolicyRepository, TenantItPolicy, UpsertItPolicy
from .repository import (
    AssetBindingError,
    InboundEventConflict,
    TicketAlreadyExists,
    TicketCapacityExceeded,
    TicketNotFound,
    TicketRepository,
    TicketVersionConflict,
    canonical_payload_hash,
    ticket_repository_context,
)
from .routing import RoutingDecision, RoutingRepository
from .sla import BusinessCalendar

__all__ = [
    "AssetBindingError",
    "BusinessCalendar",
    "CreateTicket",
    "InboundEventConflict",
    "InboundEventResult",
    "ItPolicyNotFound",
    "ItPolicyRepository",
    "OperationsConflict",
    "RoutingDecision",
    "RoutingRepository",
    "TenantItPolicy",
    "TicketAlreadyExists",
    "TicketCapacityExceeded",
    "TicketNotFound",
    "TicketOperationsRepository",
    "TicketRecord",
    "TicketRepository",
    "TicketStatusEvent",
    "TicketVersionConflict",
    "UpsertItPolicy",
    "canonical_payload_hash",
    "ticket_repository_context",
]
