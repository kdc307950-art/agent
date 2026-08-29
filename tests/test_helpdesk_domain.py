import pytest
from pydantic import ValidationError

from src.my_agent.helpdesk import (
    ActorType,
    InvalidTicketTransition,
    PendingTicketInterrupt,
    ResumeAction,
    ResumeCommandMismatch,
    TicketAction,
    TicketCommand,
    TicketPermissionDenied,
    TicketResumeCommand,
    TicketStatus,
    allowed_actions,
    transition_ticket,
    validate_resume_command,
)


def command(
    action: TicketAction,
    actor_type: ActorType,
    *,
    actor_id: str = "actor-1",
) -> TicketCommand:
    return TicketCommand(
        ticket_id="ticket-1",
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        expected_version=3,
    )


@pytest.mark.parametrize(
    ("source", "action", "actor_type", "scope", "target"),
    [
        (
            TicketStatus.NEW,
            TicketAction.START_INTAKE,
            ActorType.SYSTEM,
            "ticket:system",
            TicketStatus.INTAKING,
        ),
        (
            TicketStatus.INTAKING,
            TicketAction.REQUEST_INFORMATION,
            ActorType.AGENT,
            "ticket:agent",
            TicketStatus.AWAITING_CUSTOMER,
        ),
        (
            TicketStatus.AWAITING_CUSTOMER,
            TicketAction.PROVIDE_INFORMATION,
            ActorType.CUSTOMER,
            "ticket:customer",
            TicketStatus.INTAKING,
        ),
        (
            TicketStatus.INTAKING,
            TicketAction.CLASSIFY,
            ActorType.SYSTEM,
            "ticket:system",
            TicketStatus.CLASSIFIED,
        ),
        (
            TicketStatus.CLASSIFIED,
            TicketAction.PROPOSE_ANSWER,
            ActorType.SYSTEM,
            "ticket:system",
            TicketStatus.ANSWER_PROPOSED,
        ),
        (
            TicketStatus.ANSWER_PROPOSED,
            TicketAction.REQUEST_CONFIRMATION,
            ActorType.SYSTEM,
            "ticket:system",
            TicketStatus.AWAITING_CUSTOMER_CONFIRMATION,
        ),
        (
            TicketStatus.AWAITING_CUSTOMER_CONFIRMATION,
            TicketAction.CONFIRM_RESOLVED,
            ActorType.CUSTOMER,
            "ticket:customer",
            TicketStatus.RESOLVED,
        ),
        (
            TicketStatus.AWAITING_CUSTOMER_CONFIRMATION,
            TicketAction.REPORT_UNRESOLVED,
            ActorType.CUSTOMER,
            "ticket:customer",
            TicketStatus.QUEUED,
        ),
        (
            TicketStatus.QUEUED,
            TicketAction.ASSIGN,
            ActorType.AGENT,
            "ticket:agent",
            TicketStatus.ASSIGNED,
        ),
        (
            TicketStatus.ASSIGNED,
            TicketAction.START_WORK,
            ActorType.AGENT,
            "ticket:agent",
            TicketStatus.IN_PROGRESS,
        ),
        (
            TicketStatus.IN_PROGRESS,
            TicketAction.REQUEST_APPROVAL,
            ActorType.AGENT,
            "ticket:agent",
            TicketStatus.AWAITING_APPROVAL,
        ),
        (
            TicketStatus.AWAITING_APPROVAL,
            TicketAction.APPROVE,
            ActorType.APPROVER,
            "ticket:approve",
            TicketStatus.IN_PROGRESS,
        ),
        (
            TicketStatus.IN_PROGRESS,
            TicketAction.RESOLVE,
            ActorType.AGENT,
            "ticket:agent",
            TicketStatus.RESOLVED,
        ),
        (
            TicketStatus.RESOLVED,
            TicketAction.CLOSE,
            ActorType.SYSTEM,
            "ticket:system",
            TicketStatus.CLOSED,
        ),
        (
            TicketStatus.RESOLVED,
            TicketAction.REOPEN,
            ActorType.CUSTOMER,
            "ticket:customer",
            TicketStatus.IN_PROGRESS,
        ),
    ],
)
def test_declared_ticket_transitions(source, action, actor_type, scope, target):
    assert (
        transition_ticket(
            source,
            command(action, actor_type),
            scopes={scope},
        )
        == target
    )


def test_terminal_statuses_have_no_outgoing_transitions():
    assert dict(allowed_actions(TicketStatus.CLOSED)) == {}
    assert dict(allowed_actions(TicketStatus.CANCELLED)) == {}


@pytest.mark.parametrize(
    ("status", "action", "actor_type", "scope"),
    [
        (TicketStatus.NEW, TicketAction.CLOSE, ActorType.AGENT, "ticket:agent"),
        (TicketStatus.CLOSED, TicketAction.START_INTAKE, ActorType.SYSTEM, "ticket:system"),
        (TicketStatus.AWAITING_APPROVAL, TicketAction.RESOLVE, ActorType.AGENT, "ticket:agent"),
    ],
)
def test_illegal_state_transition_is_rejected(status, action, actor_type, scope):
    with pytest.raises(InvalidTicketTransition):
        transition_ticket(status, command(action, actor_type), scopes={scope})


def test_actor_type_and_scope_are_both_required():
    with pytest.raises(TicketPermissionDenied, match="无权"):
        transition_ticket(
            TicketStatus.AWAITING_APPROVAL,
            command(TicketAction.APPROVE, ActorType.CUSTOMER),
            scopes={"ticket:approve", "ticket:customer"},
        )

    with pytest.raises(TicketPermissionDenied, match="ticket:approve"):
        transition_ticket(
            TicketStatus.AWAITING_APPROVAL,
            command(TicketAction.APPROVE, ActorType.APPROVER),
            scopes={"ticket:agent"},
        )


def customer_pending() -> PendingTicketInterrupt:
    return PendingTicketInterrupt(
        interrupt_id="interrupt-1",
        ticket_id="ticket-1",
        expected_actor=ActorType.CUSTOMER,
        expected_actor_id="customer-1",
        allowed_actions={ResumeAction.PROVIDE_INFORMATION},
    )


def test_customer_resume_is_validated_and_converted_to_domain_command():
    resume = TicketResumeCommand(
        interrupt_id="interrupt-1",
        ticket_id="ticket-1",
        actor_type=ActorType.CUSTOMER,
        actor_id="customer-1",
        action=ResumeAction.PROVIDE_INFORMATION,
        expected_version=4,
        payload={"fields": {"device": "laptop"}},
    )

    validated = validate_resume_command(
        customer_pending(),
        resume,
        scopes={"ticket:customer"},
    )

    assert validated.ticket_command.action == TicketAction.PROVIDE_INFORMATION
    assert validated.ticket_command.expected_version == 4
    assert validated.resume_payload == {
        "action": "provide_information",
        "actor_type": "customer",
        "actor_id": "customer-1",
        "payload": {"fields": {"device": "laptop"}},
    }


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"interrupt_id": "stale"}, ResumeCommandMismatch),
        ({"ticket_id": "ticket-2"}, ResumeCommandMismatch),
        ({"actor_id": "customer-2"}, TicketPermissionDenied),
        ({"actor_type": ActorType.APPROVER}, TicketPermissionDenied),
        ({"action": ResumeAction.CONFIRM_RESOLVED}, ResumeCommandMismatch),
    ],
)
def test_resume_rejects_stale_cross_ticket_wrong_actor_and_wrong_action(changes, error):
    data = {
        "interrupt_id": "interrupt-1",
        "ticket_id": "ticket-1",
        "actor_type": ActorType.CUSTOMER,
        "actor_id": "customer-1",
        "action": ResumeAction.PROVIDE_INFORMATION,
        "expected_version": 2,
    }
    data.update(changes)
    resume = TicketResumeCommand(**data)

    with pytest.raises(error):
        validate_resume_command(
            customer_pending(), resume, scopes={"ticket:customer", "ticket:approve"}
        )


def test_customer_cannot_approve_even_when_scope_is_present():
    pending = PendingTicketInterrupt(
        interrupt_id="approval-1",
        ticket_id="ticket-1",
        expected_actor=ActorType.APPROVER,
        allowed_actions={ResumeAction.APPROVE, ResumeAction.REJECT},
    )
    resume = TicketResumeCommand(
        interrupt_id="approval-1",
        ticket_id="ticket-1",
        actor_type=ActorType.CUSTOMER,
        actor_id="customer-1",
        action=ResumeAction.APPROVE,
        expected_version=1,
    )

    with pytest.raises(TicketPermissionDenied):
        validate_resume_command(
            pending,
            resume,
            scopes={"ticket:customer", "ticket:approve"},
        )


def test_pending_interrupt_rejects_actor_action_mismatch():
    with pytest.raises(ValidationError, match="不允许由"):
        PendingTicketInterrupt(
            interrupt_id="interrupt-1",
            ticket_id="ticket-1",
            expected_actor=ActorType.CUSTOMER,
            allowed_actions={ResumeAction.APPROVE},
        )


def test_commands_reject_unknown_fields_and_negative_versions():
    with pytest.raises(ValidationError):
        TicketCommand(
            ticket_id="ticket-1",
            action=TicketAction.START_INTAKE,
            actor_type=ActorType.SYSTEM,
            actor_id="system",
            expected_version=-1,
        )

    with pytest.raises(ValidationError):
        TicketResumeCommand(
            interrupt_id="interrupt-1",
            ticket_id="ticket-1",
            actor_type=ActorType.CUSTOMER,
            actor_id="customer-1",
            action=ResumeAction.PROVIDE_INFORMATION,
            expected_version=1,
            approved=True,
        )
