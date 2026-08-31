import pytest
from pydantic import ValidationError

from backend.schema import APP_SCHEMA_VERSION, REQUIRED_RELATIONS
from backend.tickets import CreateTicket, canonical_payload_hash
from src.my_agent.helpdesk import ActorType


def test_schema_v21_requires_workflow_routing_and_it_service_relations():
    assert APP_SCHEMA_VERSION == 21
    assert {
        "tickets",
        "ticket_status_events",
        "inbound_events",
        "knowledge_documents",
        "knowledge_chunks",
        "ticket_messages",
        "outbox_events",
        "sla_policies",
        "ticket_sla",
        "satisfaction_surveys",
        "ticket_workflow_runs",
        "support_teams",
        "support_members",
        "support_schedules",
        "routing_rules",
        "ticket_assignments",
        "it_assets",
        "tenant_it_policies",
        "admin_audit_events",
        # v16: Resolution Copilot 持久化
        "copilot_runs",
        "copilot_drafts",
    }.issubset(REQUIRED_RELATIONS)


def test_canonical_payload_hash_ignores_mapping_order_but_not_values():
    first = canonical_payload_hash({"event": {"id": 1, "text": "hello"}, "channel": "wecom"})
    reordered = canonical_payload_hash({"channel": "wecom", "event": {"text": "hello", "id": 1}})
    changed = canonical_payload_hash({"channel": "wecom", "event": {"text": "changed", "id": 1}})

    assert first == reordered
    assert first != changed
    assert len(first) == 64


def test_create_ticket_validates_priority_and_rejects_unknown_fields():
    request = CreateTicket(
        ticket_id="ticket-1",
        requester_id="customer-1",
        channel="wecom",
        title="Cannot sign in",
        actor_type=ActorType.CUSTOMER,
        actor_id="customer-1",
    )
    assert request.priority == "normal"

    with pytest.raises(ValidationError):
        CreateTicket(
            ticket_id="ticket-1",
            requester_id="customer-1",
            channel="wecom",
            title="Cannot sign in",
            priority="critical",
            actor_type=ActorType.CUSTOMER,
            actor_id="customer-1",
        )

    with pytest.raises(ValidationError):
        CreateTicket(
            ticket_id="ticket-1",
            requester_id="customer-1",
            channel="wecom",
            title="Cannot sign in",
            actor_type=ActorType.CUSTOMER,
            actor_id="customer-1",
            tenant_id="attacker-controlled",
        )
