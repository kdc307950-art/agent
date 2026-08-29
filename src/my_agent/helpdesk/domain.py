"""工单领域规则：状态机、动作权限、恢复命令校验（图/API/仓储共用）。

职责：
    - TicketStatus / TicketAction / ActorType：状态、动作、参与者枚举
    - _TRANSITIONS：完整状态转移表（(当前状态, 动作) -> 目标状态）
    - _ACTION_ACTORS：每个动作允许的参与者类型（客户/坐席/审批人/系统）
    - transition_ticket / assert_actor_authorized：状态机执行 + 权限校验
    - validate_resume_command：审批/追问恢复命令的强校验（防串批/防越权）

关键设计：
    - 状态机是纯函数、无 IO，图节点、HTTP API、仓储事务都复用同一套规则，
      保证「任何入口对同一动作的判定一致」
    - MappingProxyType 只读映射：运行期不可篡改规则表
    - 恢复命令（interrupt resume）与普通流转命令分离，必须先通过
      validate_resume_command 才能转成 TicketCommand 执行
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TicketStatus(StrEnum):
    """工单生命周期状态。"""

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
    """可作用于工单的动作；合法组合由 _TRANSITIONS 决定。"""

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
    """发起动作的参与者类型，决定所需权限 scope。"""

    CUSTOMER = "customer"
    AGENT = "agent"
    APPROVER = "approver"
    SYSTEM = "system"


class ResumeAction(StrEnum):
    """挂起（interrupt）后可执行的恢复动作，与 TicketAction 一一对应。"""

    PROVIDE_INFORMATION = "provide_information"
    CONFIRM_RESOLVED = "confirm_resolved"
    REPORT_UNRESOLVED = "report_unresolved"
    APPROVE = "approve"
    REJECT = "reject"


class InvalidTicketTransition(ValueError):
    """非法状态转换：当前状态 + 动作不在转移表中。"""


class TicketPermissionDenied(PermissionError):
    """参与者类型或 scope 不满足动作要求。"""


class ResumeCommandMismatch(ValueError):
    """恢复命令与挂起记录不匹配（标识失效/不属于当前工单/动作不允许）。"""


_TRANSITIONS = MappingProxyType(
    {
        # (当前状态, 动作) -> 目标状态；只读，运行期不可改
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
        (
            TicketStatus.ANSWER_PROPOSED,
            TicketAction.REQUEST_CONFIRMATION,
        ): TicketStatus.AWAITING_CUSTOMER_CONFIRMATION,
        (TicketStatus.ANSWER_PROPOSED, TicketAction.QUEUE): TicketStatus.QUEUED,
        (
            TicketStatus.AWAITING_CUSTOMER_CONFIRMATION,
            TicketAction.CONFIRM_RESOLVED,
        ): TicketStatus.RESOLVED,
        (
            TicketStatus.AWAITING_CUSTOMER_CONFIRMATION,
            TicketAction.REPORT_UNRESOLVED,
        ): TicketStatus.QUEUED,
        (TicketStatus.AWAITING_CUSTOMER_CONFIRMATION, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.QUEUED, TicketAction.ASSIGN): TicketStatus.ASSIGNED,
        (TicketStatus.QUEUED, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (TicketStatus.ASSIGNED, TicketAction.START_WORK): TicketStatus.IN_PROGRESS,
        (TicketStatus.ASSIGNED, TicketAction.QUEUE): TicketStatus.QUEUED,
        (TicketStatus.ASSIGNED, TicketAction.CANCEL): TicketStatus.CANCELLED,
        (
            TicketStatus.IN_PROGRESS,
            TicketAction.REQUEST_INFORMATION,
        ): TicketStatus.AWAITING_CUSTOMER,
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
        # 每个动作允许的参与者类型；SYSTEM 代表受理图/Worker 内部自动执行
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
        # 参与者类型 -> 所需权限 scope（API 鉴权与状态机共用同一张表）
        ActorType.CUSTOMER: frozenset({"ticket:customer"}),
        ActorType.AGENT: frozenset({"ticket:agent"}),
        ActorType.APPROVER: frozenset({"ticket:approve"}),
        ActorType.SYSTEM: frozenset({"ticket:system"}),
    }
)

_RESUME_TO_TICKET_ACTION = MappingProxyType(
    {
        # 恢复动作 -> 对应的正式流转动作（resume 命令经校验后翻译为 TicketCommand）
        ResumeAction.PROVIDE_INFORMATION: TicketAction.PROVIDE_INFORMATION,
        ResumeAction.CONFIRM_RESOLVED: TicketAction.CONFIRM_RESOLVED,
        ResumeAction.REPORT_UNRESOLVED: TicketAction.REPORT_UNRESOLVED,
        ResumeAction.APPROVE: TicketAction.APPROVE,
        ResumeAction.REJECT: TicketAction.REJECT,
    }
)


class TicketCommand(BaseModel):
    """一次工单状态流转命令（乐观锁携带 expected_version）。

    由 API 或图节点构造，最终交给仓储层 transition_many 在事务中执行。
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    action: TicketAction
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    expected_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class PendingTicketInterrupt(BaseModel):
    """一次挂起的审批/追问：记录期望的参与者与其允许的恢复动作。

    构造时用 model_validator 校验 allowed_actions 与该参与者类型匹配，
    防止「坐席挂起的审批却允许客户批准」这类越权组合进入图执行。
    """

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
    def actions_must_match_actor(self) -> PendingTicketInterrupt:
        for action in self.allowed_actions:
            ticket_action = _RESUME_TO_TICKET_ACTION[action]
            if self.expected_actor not in _ACTION_ACTORS[ticket_action]:
                raise ValueError(f"恢复动作 {action} 不允许由 {self.expected_actor} 执行")
        return self


class TicketResumeCommand(BaseModel):
    """恢复命令（客户/审批人对 interrupt 的响应），与 TicketCommand 分离。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    action: ResumeAction
    expected_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class ValidatedResume(BaseModel):
    """校验通过的恢复结果：翻译后的流转命令 + 传给图的 resume payload。"""

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
    """校验参与者类型与 scope 是否允许执行该动作，否则抛 TicketPermissionDenied。"""
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
    """纯函数状态机：校验权限后查转移表，返回目标状态（不落库）。"""
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
    """校验恢复命令并翻译为流转命令 + 图 resume payload。

    防串批/防越权的关键检查：
        interrupt_id / ticket_id 必须匹配挂起记录（防过期标识重放）；
        actor 必须等于期望参与者（防他人代批）；action 必须在 allowed_actions 内。
    """
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
    """返回某状态下所有可执行动作及其目标状态（前端渲染操作按钮用）。"""
    return MappingProxyType(
        {action: target for (source, action), target in _TRANSITIONS.items() if source == status}
    )
