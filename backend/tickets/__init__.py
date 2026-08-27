"""Helpdesk ticket persistence package."""

from .models import CreateTicket, InboundEventResult, TicketRecord, TicketStatusEvent
from .operations import OperationsConflict, TicketOperationsRepository
from .repository import (
    InboundEventConflict,
    TicketAlreadyExists,
    TicketNotFound,
    TicketRepository,
    TicketVersionConflict,
    canonical_payload_hash,
    ticket_repository_context,
)
from .routing import RoutingDecision, RoutingRepository
from .sla import BusinessCalendar

__all__ = [
    "BusinessCalendar",
    "CreateTicket",
    "InboundEventConflict",
    "InboundEventResult",
    "OperationsConflict",
    "RoutingDecision",
    "RoutingRepository",
    "TicketAlreadyExists",
    "TicketNotFound",
    "TicketOperationsRepository",
    "TicketRecord",
    "TicketRepository",
    "TicketStatusEvent",
    "TicketVersionConflict",
    "canonical_payload_hash",
    "ticket_repository_context",
]
