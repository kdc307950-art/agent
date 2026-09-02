"""（Day 7）真实 PostgreSQL 仓储生命周期集成测试 — 不使用 FakeTickets / FakeIntakeGraph。

覆盖：建单 → 受理追问 → 补充恢复 → 分类派单 → SLA → 接单 → 处理 → 解决 →
回访 → 关闭；每次流转读取上一步 version；重复建单与版本冲突均验证。
"""

import asyncio
import os
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from backend.migrations import setup_postgres
from backend.ticket_intake import intake_config, intake_outcome_commands, pending_from_snapshot
from backend.tickets import (
    CreateTicket,
    ItPolicyRepository,
    TicketAlreadyExists,
    TicketOperationsRepository,
    TicketRepository,
    TicketVersionConflict,
    UpsertItPolicy,
)
from src.my_agent.helpdesk import (
    ActorType,
    KeywordTicketClassifier,
    ResumeAction,
    TicketAction,
    TicketCommand,
    TicketResumeCommand,
    TicketStatus,
    build_helpdesk_intake_graph,
    validate_resume_command,
)

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_real_repository_lifecycle_http_style(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:10]}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        operations = TicketOperationsRepository(tickets.pool)
        it_policies = ItPolicyRepository(tickets.pool)
        try:
            async with tickets.pool.connection() as connection:
                await connection.execute(
                    """
                    INSERT INTO sla_policies (
                        tenant_id, policy_id, name, timezone, business_days,
                        work_start, work_end, first_response_minutes, resolution_minutes
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (tenant, "sla-vpn", "VPN SLA", "UTC", [0, 1, 2, 3, 4], time(9), time(18), 15, 120),
                )
            await it_policies.upsert(
                tenant,
                UpsertItPolicy(
                    category="it.vpn",
                    policy_id="sla-vpn",
                    required_fields=("device",),
                    default_priority="normal",
                ),
            )

            graph = build_helpdesk_intake_graph(
                classifier=KeywordTicketClassifier(),
                checkpointer=MemorySaver(),
                it_policy_provider=it_policies,
            )
            await tickets.create(
                tenant,
                CreateTicket(
                    ticket_id=ticket_id,
                    requester_id="customer-1",
                    channel="web",
                    title="VPN 无法连接",
                    description="error 809",
                    actor_type=ActorType.CUSTOMER,
                    actor_id="customer-1",
                ),
            )
            ticket = await tickets.get(tenant, ticket_id)
            assert ticket.status == TicketStatus.NEW
            assert ticket.version == 0
            config = intake_config(
                tenant,
                ticket_id,
                user_id="customer-1",
                departments=("it",),
            )
            run = await tickets.start_workflow_operation(
                tenant_id=tenant,
                ticket_id=ticket_id,
                operation_id="op-life",
                command_type="intake",
                expected_version=ticket.version,
                checkpoint_thread_id=config["configurable"]["thread_id"],
            )
            assert run["status"] == "started"

            result = await graph.ainvoke(
                {
                    "ticket_id": ticket_id,
                    "requester_id": "customer-1",
                    "text": "VPN 无法连接，错误码 809",
                    "fields": {
                        "title": "VPN",
                        "description": "error 809",
                        "requester_id": "customer-1",
                    },
                    "clarification_rounds": 0,
                },
                config,
            )
            assert "__interrupt__" in result
            assert "device" in result.get("missing_fields", [])

            first_commands = [
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.START_INTAKE,
                    actor_type=ActorType.SYSTEM,
                    actor_id="intake-agent",
                    expected_version=0,
                ),
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.REQUEST_INFORMATION,
                    actor_type=ActorType.SYSTEM,
                    actor_id="intake-agent",
                    expected_version=1,
                    payload={"missing_fields": ["device"]},
                ),
            ]
            await tickets.record_workflow_intent(
                tenant_id=tenant,
                ticket_id=ticket_id,
                operation_id="op-life",
                intent={"commands": [c.model_dump(mode="json") for c in first_commands], "result": {}},
            )
            ticket = await tickets.transition_many(
                tenant,
                first_commands,
                scopes={"ticket:system"},
                operation_id="op-life",
            )
            assert ticket.status == TicketStatus.AWAITING_CUSTOMER
            assert ticket.version == 2

            snapshot = await graph.aget_state(config)
            interrupt_id = str(snapshot.tasks[0].interrupts[0].id)
            pending = pending_from_snapshot(snapshot, interrupt_id)
            resume_command = TicketResumeCommand(
                interrupt_id=interrupt_id,
                ticket_id=ticket_id,
                actor_type=ActorType.CUSTOMER,
                actor_id="customer-1",
                action=ResumeAction.PROVIDE_INFORMATION,
                expected_version=ticket.version,
                payload={"fields": {"device": "laptop-001"}},
            )
            validated = validate_resume_command(
                pending, resume_command, scopes={"ticket:customer", "ticket:system"}
            )
            resumed = await graph.ainvoke(Command(resume=validated.resume_payload), config)
            second_commands = [
                validated.ticket_command,
                *intake_outcome_commands(
                    ticket_id=ticket_id,
                    actor_id="intake-agent",
                    expected_version=ticket.version + 1,
                    result=resumed,
                ),
            ]
            ticket = await tickets.transition_many(
                tenant,
                second_commands,
                scopes={"ticket:system"},
                operation_id="op-life-resume",
            )
            assert ticket.status == TicketStatus.QUEUED
            assert ticket.version == 5

            created_sla = await operations.ensure_sla_for_ticket(
                tenant_id=tenant, ticket_id=ticket_id, category="it.vpn"
            )
            assert created_sla is True

            ticket = await tickets.transition(
                tenant,
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.ASSIGN,
                    actor_type=ActorType.AGENT,
                    actor_id="agent-1",
                    expected_version=ticket.version,
                ),
                scopes={"ticket:agent"},
            )
            assert ticket.status == TicketStatus.ASSIGNED
            in_progress = await tickets.transition(
                tenant,
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.START_WORK,
                    actor_type=ActorType.AGENT,
                    actor_id="agent-1",
                    expected_version=ticket.version,
                ),
                scopes={"ticket:agent"},
            )
            assert in_progress.status == TicketStatus.IN_PROGRESS
            resolved = await tickets.transition(
                tenant,
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.RESOLVE,
                    actor_type=ActorType.AGENT,
                    actor_id="agent-1",
                    expected_version=in_progress.version,
                ),
                scopes={"ticket:agent"},
            )
            assert resolved.status == TicketStatus.RESOLVED

            survey_id = f"survey-{uuid4().hex[:8]}"
            created_survey = await operations.create_survey(
                tenant_id=tenant,
                ticket_id=ticket_id,
                survey_id=survey_id,
                expires_at=datetime.now(UTC) + timedelta(days=7),
                outbox_event_id=f"survey-out-{uuid4().hex[:8]}",
            )
            assert created_survey is True
            responded = await operations.respond_survey(
                tenant_id=tenant, survey_id=survey_id, ticket_id=ticket_id, score=5, feedback="ok"
            )
            assert responded is True

            closed = await tickets.transition(
                tenant,
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.CLOSE,
                    actor_type=ActorType.AGENT,
                    actor_id="agent-1",
                    expected_version=resolved.version,
                ),
                scopes={"ticket:agent"},
            )
            assert closed.status == TicketStatus.CLOSED

            # 重复建单与版本冲突
            with pytest.raises(TicketAlreadyExists):
                await tickets.create(
                    tenant,
                    CreateTicket(
                        ticket_id=ticket_id,
                        requester_id="customer-1",
                        channel="web",
                        title="duplicate",
                        actor_type=ActorType.CUSTOMER,
                        actor_id="customer-1",
                    ),
                )
            with pytest.raises(TicketVersionConflict):
                await tickets.transition(
                    tenant,
                    TicketCommand(
                        ticket_id=ticket_id,
                        action=TicketAction.ASSIGN,
                        actor_type=ActorType.AGENT,
                        actor_id="agent-1",
                        expected_version=0,
                    ),
                    scopes={"ticket:agent"},
                )
            return closed
        finally:
            await tickets.close()

    closed = asyncio.run(run())
    assert closed.version == 9
