"""Helpdesk domain rules shared by graphs, APIs, and repositories."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TicketStatus(StrEnum):
    NEW = "new"
    INTAKING = "intaking"
    AWAITING_CUSTOMER = "awaiting_customer"
    CLASSIFIED = "classified"
    ANSWER_PROPOSED = "answer_proposed"
    AWAITING_CUSTOMER_CONFIRMATION = "awaiting_customer_confirmation"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class TicketAction(StrEnum):
    START_INTAKE = "start_intake"
    REQUEST_INFORMATION = "request_information"
    PROVIDE_INFORMATION = "provide_information"
    CLASSIFY = "classify"
    PROPOSE_ANSWER = "propose_answer"
    REQUEST_CONFIRMATION = "request_confirmation"
    CONFIRM_RESOLVED = "confirm_resolved"
    REPORT_UNRESOLVED = "report_unresolved"
    QUEUE = "queue"
    ASSIGN = "assign"
    START_WORK = "start_work"
    REQUEST_APPROVAL = "request_approval"
    APPROVE = "approve"
    REJECT = "reject"
    RESOLVE = "resolve"
    REOPEN = "reopen"
    CLOSE = "close"
    CANCEL = "cancel"


class ActorType(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    APPROVER = "approver"
    SYSTEM = "system"


class ResumeAction(StrEnum):
    PROVIDE_INFORMATION = "provide_information"
    CONFIRM_RESOLVED = "confirm_resolved"
    REPORT_UNRESOLVED = "report_unresolved"
    APPROVE = "approve"
    REJECT = "reject"


class InvalidTicketTransition(ValueError):
    pass


class TicketPermissionDenied(PermissionError):
    pass


class ResumeCommandMismatch(ValueError):
    pass


_TRANSITIONS = MappingProxyType(
    {
        (TicketStatus.NEW, TicketAction.START_INTAKE): TicketStatus.INTAKING,
        (TicketStatus.NEW, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.INTAKING, TicketAction.REQUEST_INFORMATION): TicketStatus.AWAITING_CUSTOMER,
        (TicketStatus.INTAKING, TicketAction.CLASSIFY): TicketStatus.CLASSIFIED,
        (TicketStatus.INTAKING, TicketAction.QUEUE): TicketStatus.QUEUED,
        (TicketStatus.INTAKING, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.AWAITING_CUSTOMER, TicketAction.PROVIDE_INFORMATION): TicketStatus.INTAKING,
        (TicketStatus.AWAITING_CUSTOMER, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.CLASSIFIED, TicketAction.PROPOSE_ANSWER): TicketStatus.ANSWER_PROPOSED,
        (TicketStatus.CLASSIFIED, TicketAction.QUEUE): TicketStatus.QUEUED,
        (TicketStatus.CLASSIFIED, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.ANSWER_PROPOSED, TicketAction.REQUEST_CONFIRMATION): TicketStatus.AWAITING_CUSTOMER_CONFIRMATION,
        (TicketStatus.ANSWER_PROPOSED, TicketAction.QUEUE): TicketStatus.QUEUED,
        (TicketStatus.AWAITING_CUSTOMER_CONFIRMATION, TicketAction.CONFIRM_RESOLVED): TicketStatus.RESOLVED,
        (TicketStatus.AWAITING_CUSTOMER_CONFIRMATION, TicketAction.REPORT_UNRESOLVED): TicketStatus.QUEUED,
        (TicketStatus.AWAITING_CUSTOMER_CONFIRMATION, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.QUEUED, TicketAction.ASSIGN): TicketStatus.ASSIGNED,
        (TicketStatus.QUEUED, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.ASSIGNED, TicketAction.START_WORK): TicketStatus.IN_PROGRESS,
        (TicketStatus.ASSIGNED, TicketAction.QUEUE): TicketStatus.QUEUED,
        (TicketStatus.ASSIGNED, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.IN_PROGRESS, TicketAction.REQUEST_INFORMATION): TicketStatus.AWAITING_CUSTOMER,
        (TicketStatus.IN_PROGRESS, TicketAction.REQUEST_APPROVAL): TicketStatus.AWAITING_APPROVAL,
        (TicketStatus.IN_PROGRESS, TicketAction.RESOLVE): TicketStatus.RESOLVED,
        (TicketStatus.IN_PROGRESS, TicketAction.QUEUE): TicketStatus.QUEUED,
        (TicketStatus.IN_PROGRESS, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.AWAITING_APPROVAL, TicketAction.APPROVE): TicketStatus.IN_PROGRESS,
        (TicketStatus.AWAITING_APPROVAL, TicketAction.REJECT): TicketStatus.IN_PROGRESS,
        (TicketStatus.AWAITING_APPROVAL, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.RESOLVED, TicketAction.CLOSE): TicketStatus.CLOSED,
        (TicketStatus.RESOLVED, TicketAction.REOPEN): TicketStatus.IN_PROGRESS,
    }
)

_ACTION_ACTORS = MappingProxyType(
    {
        TicketAction.START_INTAKE: frozenset({ActorType.SYSTEM, ActorType.AGENT}),
        TicketAction.REQUEST_INFORMATION: frozenset({ActorType.SYSTEM, ActorType.AGENT}),
        TicketAction.PROVIDE_INFORMATION: frozenset({ActorType.CUSTOMER}),
        TicketAction.CLASSIFY: frozenset({ActorType.SYSTEM, ActorType.AGENT}),
        TicketAction.PROPOSE_ANSWER: frozenset({ActorType.SYSTEM, ActorType.AGENT}),
        TicketAction.REQUEST_CONFIRMATION: frozenset({ActorType.SYSTEM, ActorType.AGENT}),
        TicketAction.CONFIRM_RESOLVED: frozenset({ActorType.CUSTOMER}),
        TicketAction.REPORT_UNRESOLVED: frozenset({ActorType.CUSTOMER}),
        TicketAction.QUEUE: frozenset({ActorType.SYSTEM, ActorType.AGENT}),
        TicketAction.ASSIGN: frozenset({ActorType.SYSTEM, ActorType.AGENT}),
        TicketAction.START_WORK: frozenset({ActorType.AGENT}),
        TicketAction.REQUEST_APPROVAL: frozenset({ActorType.AGENT}),
        TicketAction.APPROVE: frozenset({ActorType.APPROVER}),
        TicketAction.REJECT: frozenset({ActorType.APPROVER}),
        TicketAction.RESOLVE: frozenset({ActorType.AGENT}),
        TicketAction.REOPEN: frozenset({ActorType.CUSTOMER, ActorType.AGENT}),
        TicketAction.CLOSE: frozenset({ActorType.SYSTEM, ActorType.AGENT}),
        TicketAction.CANCEL: frozenset({ActorType.CUSTOMER, ActorType.AGENT}),
    }
)

_ACTOR_SCOPES = MappingProxyType(
    {
        ActorType.CUSTOMER: frozenset({"ticket:customer"}),
        ActorType.AGENT: frozenset({"ticket:agent"}),
        ActorType.APPROVER: frozenset({"ticket:approve"}),
        ActorType.SYSTEM: frozenset({"ticket:system"}),
    }
)

_RESUME_TO_TICKET_ACTION = MappingProxyType(
    {
        ResumeAction.PROVIDE_INFORMATION: TicketAction.PROVIDE_INFORMATION,
        ResumeAction.CONFIRM_RESOLVED: TicketAction.CONFIRM_RESOLVED,
        ResumeAction.REPORT_UNRESOLVED: TicketAction.REPORT_UNRESOLVED,
        ResumeAction.APPROVE: TicketAction.APPROVE,
        ResumeAction.REJECT: TicketAction.REJECT,
    }
)


class TicketCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    action: TicketAction
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    expected_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class PendingTicketInterrupt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    expected_actor: ActorType
    expected_actor_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    allowed_actions: frozenset[ResumeAction] = Field(min_length=1)

    @model_validator(mode="after")
    def actions_must_match_actor(self) -> "PendingTicketInterrupt":
        for action in self.allowed_actions:
            ticket_action = _RESUME_TO_TICKET_ACTION[action]
            if self.expected_actor not in _ACTION_ACTORS[ticket_action]:
                raise ValueError(
                    f"恢复动作 {action} 不允许由 {self.expected_actor} 执行"
                )
        return self


class TicketResumeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    action: ResumeAction
    expected_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class ValidatedResume(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticket_command: TicketCommand
    resume_payload: dict[str, Any]


def required_scopes(actor_type: ActorType) -> frozenset[str]:
    return _ACTOR_SCOPES[actor_type]


def assert_actor_authorized(
    action: TicketAction,
    actor_type: ActorType,
    scopes: Iterable[str],
) -> None:
    if actor_type not in _ACTION_ACTORS[action]:
        raise TicketPermissionDenied(f"参与者 {actor_type} 无权执行动作 {action}")
    missing = required_scopes(actor_type).difference(scopes)
    if missing:
        raise TicketPermissionDenied(f"缺少权限: {', '.join(sorted(missing))}")


def transition_ticket(
    status: TicketStatus,
    command: TicketCommand,
    *,
    scopes: Iterable[str],
) -> TicketStatus:
    assert_actor_authorized(command.action, command.actor_type, scopes)
    target = _TRANSITIONS.get((status, command.action))
    if target is None:
        raise InvalidTicketTransition(f"不允许的工单状态转换: {status} + {command.action}")
    return target


def validate_resume_command(
    pending: PendingTicketInterrupt,
    command: TicketResumeCommand,
    *,
    scopes: Iterable[str],
) -> ValidatedResume:
    if command.interrupt_id != pending.interrupt_id:
        raise ResumeCommandMismatch("恢复标识已失效")
    if command.ticket_id != pending.ticket_id:
        raise ResumeCommandMismatch("恢复命令不属于当前工单")
    if command.actor_type != pending.expected_actor:
        raise TicketPermissionDenied("当前参与者不能恢复该挂起任务")
    if pending.expected_actor_id is not None and command.actor_id != pending.expected_actor_id:
        raise TicketPermissionDenied("当前用户不能恢复该挂起任务")
    if command.action not in pending.allowed_actions:
        raise ResumeCommandMismatch("当前挂起任务不允许该恢复动作")

    ticket_action = _RESUME_TO_TICKET_ACTION[command.action]
    assert_actor_authorized(ticket_action, command.actor_type, scopes)
    ticket_command = TicketCommand(
        ticket_id=command.ticket_id,
        action=ticket_action,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        expected_version=command.expected_version,
        payload=command.payload,
    )
    return ValidatedResume(
        ticket_command=ticket_command,
        resume_payload={
            "action": command.action.value,
            "actor_type": command.actor_type.value,
            "actor_id": command.actor_id,
            "payload": command.payload,
        },
    )


def allowed_actions(status: TicketStatus) -> Mapping[TicketAction, TicketStatus]:
    return MappingProxyType(
        {
            action: target
            for (source, action), target in _TRANSITIONS.items()
            if source == status
        }
    )
