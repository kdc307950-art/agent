import asyncio
import os
from uuid import uuid4

import pytest

from backend.migrations import setup_postgres
from backend.tickets import (
    CreateTicket,
    InboundEventConflict,
    TicketRepository,
    TicketVersionConflict,
)
from src.my_agent.helpdesk import ActorType, TicketAction, TicketCommand, TicketStatus

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def create_request(ticket_id: str, *, external_ticket_id: str | None = None) -> CreateTicket:
    return CreateTicket(
        ticket_id=ticket_id,
        requester_id="customer-1",
        channel="wecom",
        external_ticket_id=external_ticket_id,
        title="Cannot sign in",
        description="SSO returns an error",
        actor_type=ActorType.CUSTOMER,
        actor_id="customer-1",
    )


def test_ticket_create_transition_is_tenant_scoped_and_optimistically_locked(monkeypatch):
    tenant_id = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        repository = await TicketRepository.connect(DATABASE_URL)
        try:
            created = await repository.create(tenant_id, create_request(ticket_id))
            foreign = await repository.get("another-tenant", ticket_id)
            transitioned = await repository.transition(
                tenant_id,
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.START_INTAKE,
                    actor_type=ActorType.SYSTEM,
                    actor_id="intake-worker",
                    expected_version=0,
                ),
                scopes={"ticket:system"},
            )
            batched = await repository.transition_many(
                tenant_id,
                [
                    TicketCommand(
                        ticket_id=ticket_id,
                        action=TicketAction.CLASSIFY,
                        actor_type=ActorType.SYSTEM,
                        actor_id="intake-worker",
                        expected_version=1,
                        payload={"category": "it"},
                    ),
                    TicketCommand(
                        ticket_id=ticket_id,
                        action=TicketAction.QUEUE,
                        actor_type=ActorType.SYSTEM,
                        actor_id="intake-worker",
                        expected_version=2,
                        payload={"team_id": "team-it", "priority": "high"},
                    ),
                ],
                scopes={"ticket:system"},
            )
            with pytest.raises(TicketVersionConflict):
                await repository.transition(
                    tenant_id,
                    TicketCommand(
                        ticket_id=ticket_id,
                        action=TicketAction.START_INTAKE,
                        actor_type=ActorType.SYSTEM,
                        actor_id="intake-worker",
                        expected_version=0,
                    ),
                    scopes={"ticket:system"},
                )
            events = await repository.list_status_events(tenant_id, ticket_id)
            return created, foreign, transitioned, batched, events
        finally:
            await repository.close()

    created, foreign, transitioned, batched, events = asyncio.run(run())
    assert created.status == TicketStatus.NEW
    assert created.version == 0
    assert foreign is None
    assert transitioned.status == TicketStatus.INTAKING
    assert transitioned.version == 1
    assert batched.status == TicketStatus.QUEUED
    assert batched.version == 3
    assert batched.category == "it"
    assert batched.assigned_team_id == "team-it"
    assert batched.priority == "high"
    assert [(event.action, event.ticket_version) for event in events] == [
        ("create", 0),
        ("start_intake", 1),
        ("classify", 2),
        ("queue", 3),
    ]


def test_workflow_operation_commits_intent_once_and_retries_idempotently(monkeypatch):
    tenant_id = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        repository = await TicketRepository.connect(DATABASE_URL)
        try:
            await repository.create(tenant_id, create_request(ticket_id))
            run = await repository.start_workflow_operation(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                operation_id="operation-intake-1",
                command_type="intake",
                expected_version=0,
                checkpoint_thread_id=f"helpdesk:{tenant_id}:{ticket_id}",
            )
            commands = [
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.START_INTAKE,
                    actor_type=ActorType.SYSTEM,
                    actor_id="worker",
                    expected_version=0,
                ),
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.CLASSIFY,
                    actor_type=ActorType.SYSTEM,
                    actor_id="worker",
                    expected_version=1,
                    payload={"category": "it"},
                ),
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.QUEUE,
                    actor_type=ActorType.SYSTEM,
                    actor_id="worker",
                    expected_version=2,
                    payload={"team_id": "team-it"},
                ),
            ]
            await repository.record_workflow_intent(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                operation_id="operation-intake-1",
                intent={"commands": [command.model_dump(mode="json") for command in commands]},
            )
            committed = await repository.transition_many(
                tenant_id, commands, scopes={"ticket:system"}, operation_id="operation-intake-1"
            )
            retried = await repository.transition_many(
                tenant_id, commands, scopes={"ticket:system"}, operation_id="operation-intake-1"
            )
            operation = await repository.get_workflow_operation(
                tenant_id=tenant_id, ticket_id=ticket_id, operation_id="operation-intake-1"
            )
            events = await repository.list_status_events(tenant_id, ticket_id)
            return run, committed, retried, operation, events
        finally:
            await repository.close()

    run, committed, retried, operation, events = asyncio.run(run())
    assert run["status"] == "started"
    assert committed.version == 3
    assert retried.version == 3
    assert operation["status"] == "committed"
    assert [event.ticket_version for event in events] == [0, 1, 2, 3]


def test_inbound_event_registration_is_idempotent_and_detects_payload_reuse(monkeypatch):
    tenant_id = f"tenant-{uuid4().hex}"
    event_id = f"event-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        repository = await TicketRepository.connect(DATABASE_URL)
        try:
            first = await repository.register_inbound_event(
                tenant_id,
                "wecom",
                event_id,
                {"message": {"id": 1, "text": "help"}},
            )
            duplicate = await repository.register_inbound_event(
                tenant_id,
                "wecom",
                event_id,
                {"message": {"text": "help", "id": 1}},
            )
            await repository.create(tenant_id, create_request(ticket_id))
            await repository.attach_inbound_event(tenant_id, "wecom", event_id, ticket_id)
            attached = await repository.register_inbound_event(
                tenant_id,
                "wecom",
                event_id,
                {"message": {"id": 1, "text": "help"}},
            )
            with pytest.raises(InboundEventConflict):
                await repository.register_inbound_event(
                    tenant_id,
                    "wecom",
                    event_id,
                    {"message": {"id": 1, "text": "different"}},
                )
            other_tenant = await repository.register_inbound_event(
                "another-tenant",
                "wecom",
                event_id,
                {"message": {"id": 1, "text": "different"}},
            )
            return first, duplicate, attached, other_tenant
        finally:
            await repository.close()

    first, duplicate, attached, other_tenant = asyncio.run(run())
    assert first.created is True
    assert duplicate.created is False
    assert duplicate.payload_hash == first.payload_hash
    assert attached.created is False
    assert attached.ticket_id == ticket_id
    assert other_tenant.created is True
