import asyncio
import os
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

import pytest

from backend.migrations import setup_postgres
from backend.tickets import BusinessCalendar, CreateTicket, TicketOperationsRepository, TicketRepository
from src.my_agent.helpdesk import ActorType


DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_outbox_sla_and_survey_lifecycle(monkeypatch):
    tenant_id = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        operations = TicketOperationsRepository(tickets.pool)
        try:
            await tickets.create(
                tenant_id,
                CreateTicket(
                    ticket_id=ticket_id,
                    requester_id="customer-1",
                    channel="wecom",
                    title="Login failure",
                    actor_type=ActorType.CUSTOMER,
                    actor_id="customer-1",
                ),
            )
            async with tickets.pool.connection() as connection:
                await connection.execute(
                    """
                    INSERT INTO sla_policies (
                        tenant_id, policy_id, name, timezone, business_days,
                        work_start, work_end, first_response_minutes, resolution_minutes
                    ) VALUES (%s, 'default', 'Default', 'UTC', %s, %s, %s, 30, 120)
                    """,
                    (tenant_id, [0, 1, 2, 3, 4], time(9), time(18)),
                )
            first = await operations.append_outbound_message(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                message_id="message-1",
                actor_type="system",
                actor_id="agent",
                channel="wecom",
                content="We received your request",
                event_id="event-1",
                idempotency_key=f"ticket:{ticket_id}:received",
                payload={"ticket_id": ticket_id, "content": "We received your request"},
            )
            duplicate = await operations.append_outbound_message(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                message_id="message-duplicate",
                actor_type="system",
                actor_id="agent",
                channel="wecom",
                content="duplicate",
                event_id="event-duplicate",
                idempotency_key=f"ticket:{ticket_id}:received",
                payload={"ticket_id": ticket_id},
            )
            claimed = await operations.claim_outbox(limit=10, tenant_id=tenant_id)
            completed = await operations.complete_outbox(tenant_id, "event-1")

            calendar = BusinessCalendar(
                timezone_name="UTC",
                business_days=frozenset({0, 1, 2, 3, 4}),
                work_start=time(9),
                work_end=time(18),
            )
            started = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
            await operations.create_sla(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                policy_id="default",
                policy_version=1,
                started_at=started,
                first_response_minutes=30,
                resolution_minutes=120,
                calendar=calendar,
            )
            paused = await operations.pause_sla(tenant_id, ticket_id, reason="awaiting_customer")
            async with tickets.pool.connection() as connection:
                await connection.execute(
                    "UPDATE ticket_sla SET paused_at = %s WHERE tenant_id = %s AND ticket_id = %s",
                    (started + timedelta(minutes=30), tenant_id, ticket_id),
                )
            resumed = await operations.resume_sla(
                tenant_id,
                ticket_id,
                resumed_at=started + timedelta(minutes=90),
            )
            scan_at = datetime.now(timezone.utc)
            async with tickets.pool.connection() as connection:
                await connection.execute(
                    """
                    UPDATE ticket_sla
                    SET first_response_due_at = %s, resolution_due_at = %s
                    WHERE tenant_id = %s AND ticket_id = %s
                    """,
                    (scan_at - timedelta(minutes=2), scan_at - timedelta(minutes=1), tenant_id, ticket_id),
                )
            first_scan = await operations.scan_sla_breaches(now=scan_at, tenant_id=tenant_id)
            second_scan = await operations.scan_sla_breaches(now=scan_at, tenant_id=tenant_id)

            survey_created = await operations.create_survey(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                survey_id="survey-1",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                outbox_event_id="survey-event-1",
            )
            survey_duplicate = await operations.create_survey(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                survey_id="survey-2",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                outbox_event_id="survey-event-2",
            )
            responded = await operations.respond_survey(
                tenant_id=tenant_id,
                survey_id="survey-1",
                score=5,
                feedback="resolved",
            )
            return (
                first,
                duplicate,
                claimed,
                completed,
                paused,
                resumed,
                first_scan,
                second_scan,
                survey_created,
                survey_duplicate,
                responded,
            )
        finally:
            await tickets.close()

    result = asyncio.run(run())
    (
        first,
        duplicate,
        claimed,
        completed,
        paused,
        resumed,
        first_scan,
        second_scan,
        survey_created,
        survey_duplicate,
        responded,
    ) = result
    assert first is True
    assert duplicate is False
    assert [item["event_id"] for item in claimed] == ["event-1"]
    assert completed is True
    assert paused is True
    assert resumed is True
    assert first_scan == 2
    assert second_scan == 0
    assert survey_created is True
    assert survey_duplicate is False
    assert responded is True
