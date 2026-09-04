"""LANGGraph VPN Diagnosis Agent — 业务层校验 + 执行（命令→工单状态机映射）。

模块归属：backend/vpn。设计要点（对齐 docs/product/vpn-diagnosis-agent-contract.md §8）：
    - 两层校验：① 集合校验（command 必须在允许集合）；② 禁止命令拒绝
      （命中 FORBIDDEN_COMMANDS 或未注册命令 -> 抛 DiagnosisCommandRejected + 审计，
      不产生任何副作用、不发消息、不改工单状态、不调用业务动作）。
    - 命令 → domain.TicketAction 映射表（ask_customer→REQUEST_INFORMATION /
      assign_agent→ASSIGN / request_approval→REQUEST_APPROVAL /
      escalate_incident→QUEUE 转人工队列 / provide_steps→仅草稿不发送）。
    - 与 domain.transition_ticket / assert_actor_authorized 走同一张状态机与权限表，
      任何入口对同一命令判定一致；禁止命令一律 ok=False 且零副作用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.my_agent.helpdesk import ActorType, TicketAction, TicketCommand

from .models import ALLOWED_COMMANDS, FORBIDDEN_COMMANDS, DiagnosisCommand, DiagnosisCommandType

logger = logging.getLogger("langgraph.vpn")

# 命令 → domain.TicketAction 映射（契约 §8.2）。
COMMAND_TO_ACTION: dict[DiagnosisCommandType, TicketAction] = {
    DiagnosisCommandType.ASK_CUSTOMER: TicketAction.REQUEST_INFORMATION,
    DiagnosisCommandType.PROVIDE_STEPS: TicketAction.PROPOSE_ANSWER,
    DiagnosisCommandType.ASSIGN_AGENT: TicketAction.ASSIGN,
    DiagnosisCommandType.ESCALATE_INCIDENT: TicketAction.QUEUE,
    DiagnosisCommandType.REQUEST_APPROVAL: TicketAction.REQUEST_APPROVAL,
}

# 仅产出草稿（不直接发消息）的命令：provide_steps。
DRAFT_ONLY_COMMANDS: frozenset[str] = frozenset({"provide_steps"})
# 转人工队列且禁止自动回复的命令：escalate_incident。
NO_AUTO_REPLY_COMMANDS: frozenset[str] = frozenset({"escalate_incident"})
# 升级事件到达的人工接管队列（escalate_incident 专用）。
HUMAN_QUEUE_TEAM_ID = "team-service-desk"


class DiagnosisCommandRejected(ValueError):
    """DiagnosisCommand 被业务层拒绝：禁止命令 / 未注册命令 / 模型已校验非法。

    携带 reason 供上层返回给调用方并留审计。
    """

    def __init__(self, command: str, reason: str) -> None:
        self.command = command
        self.reason = reason
        super().__init__(f"VPN 命令被拒绝: {command} ({reason})")


@dataclass(frozen=True, slots=True)
class ExecResult:
    """一次 DiagnosisCommand 执行的结果。

    ok       : 是否成功执行（允许命令全部成功；禁止/非法命令为 False）
    reason   : 失败原因（forbidden_command / not_in_allowed_set / 状态机拒绝 / ...）
    action   : 映射到的 domain.TicketAction（已成功执行的命令才有）
    ticket_id: 目标工单
    draft    : provide_steps 的草稿文本（仅草稿，不直接发送）
    detail   : 补充说明（如 escalate 进入的人工队列）
    """

    ok: bool
    reason: str | None = None
    action: str | None = None
    ticket_id: str | None = None
    draft: str | None = None
    detail: str | None = None


class DiagnosisCommandValidator:
    """DiagnosisCommand 校验器：集合校验 + 禁止命令拒绝（无副作用）。

    validate 抛 DiagnosisCommandRejected 表示拒绝；返回 None 表示通过。
    """

    def __init__(self) -> None:
        self.allowed = set(ALLOWED_COMMANDS)
        self.forbidden = set(FORBIDDEN_COMMANDS)

    def validate(self, command: DiagnosisCommand) -> None:
        raw = str(command.command)
        if raw in self.forbidden:
            raise DiagnosisCommandRejected(raw, "forbidden_command")
        if raw not in self.allowed:
            raise DiagnosisCommandRejected(raw, "not_in_allowed_set")

    def to_ticket_action(self, command: DiagnosisCommand) -> TicketAction:
        return COMMAND_TO_ACTION[command.command]


async def _record_rejection(
    runtime: Any,
    run_context: Any,
    command: DiagnosisCommand,
    reason: str,
) -> None:
    """留审计（经 runtime.audit / tool_governance 或直接 AuditRepository.record_event）。

    审计写入失败不阻断主流程（仅记录日志）。
    """
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    try:
        await audit.record_event(
            run_context,
            "vpn_command_rejected",
            status="denied",
            payload={"command": str(command.command), "reason": reason},
        )
    except Exception as exc:
        logger.warning("VPN 命令拒绝审计写入失败: %s", type(exc).__name__)


async def execute_diagnosis_command(
    command: DiagnosisCommand,
    *,
    runtime: Any,
    run_context: Any,
    ticket_id: str | None = None,
    transition: Any | None = None,
) -> ExecResult:
    """校验并执行一条 DiagnosisCommand。

    参数：
        command   : 已通过 DiagnosisCommand 模型校验的结构化命令
        runtime   : AgentRuntime（提供 tickets / audit / ticket_operations）
        run_context: RunContext（服务端身份/租户/scopes，用于授权与审计）
        ticket_id : 目标工单（默认取自 command.payload["ticket_id"]）
        transition: 可选注入的状态机执行器（测试用）；缺省用 runtime.tickets.transition

    任何禁止/非法命令都会被拒绝（抛 DiagnosisCommandRejected 并留审计），零副作用。
    """
    validator = DiagnosisCommandValidator()
    # 1) 集合与禁止校验
    validator.validate(command)
    ticket_id = ticket_id or str(command.payload.get("ticket_id") or "")
    if not ticket_id:
        raise DiagnosisCommandRejected(str(command.command), "missing_ticket_id")

    action = validator.to_ticket_action(command)
    tenant_id = getattr(run_context, "tenant_id", None)
    actor_id = getattr(run_context, "user_id", None) or "vpn-agent"
    scopes = set(getattr(run_context, "scopes", frozenset()) or ())

    # 2) 草稿-only 命令：仅产出草稿，不直接发消息、不改状态
    if str(command.command) in DRAFT_ONLY_COMMANDS:
        return ExecResult(
            ok=True,
            reason="draft_only",
            action=action.value,
            ticket_id=ticket_id,
            draft=command.content or "（排查步骤草稿，未发送）",
            detail="provide_steps 仅生成草稿，不直接发送消息",
        )

    # 3) 组装 TicketCommand（actor_type=AGENT，作用域 ticket:agent）
    ticket_cmd = TicketCommand(
        ticket_id=ticket_id,
        action=action,
        actor_type=ActorType.AGENT,
        actor_id=actor_id,
        expected_version=_expected_version(command, default=0),
        payload=dict(command.payload),
    )

    # 4) 执行映射：escalate_incident 进人工队列（team-service-desk），禁止自动回复
    if str(command.command) in NO_AUTO_REPLY_COMMANDS:
        ticket_cmd = _with_team_queue(ticket_cmd)

    # 5) 经状态机执行（transition_ticket / 仓储 transition，乐观锁+scope）
    try:
        runner = transition
        if runner is None:
            runner = runtime.tickets.transition
        await runner(tenant_id, ticket_cmd, scopes=scopes)
    except Exception as exc:
        logger.warning("VPN 命令执行失败 %s: %s", str(command.command), type(exc).__name__)
        reason = _map_transition_error(exc)
        # 失败也留审计
        try:
            await _record_rejection(runtime, run_context, command, reason)
        except Exception:
            pass
        return ExecResult(ok=False, reason=reason, action=action.value, ticket_id=ticket_id)

    return ExecResult(
        ok=True,
        action=action.value,
        ticket_id=ticket_id,
        detail=(
            f"已进入人工队列 {HUMAN_QUEUE_TEAM_ID}，不自动回复"
            if str(command.command) in NO_AUTO_REPLY_COMMANDS
            else None
        ),
    )


def _expected_version(command: DiagnosisCommand, default: int) -> int:
    value = command.payload.get("expected_version")
    if isinstance(value, int) and value >= 0:
        return value
    return default


def _with_team_queue(ticket_cmd: TicketCommand) -> TicketCommand:
    payload = dict(ticket_cmd.payload)
    payload["team_id"] = HUMAN_QUEUE_TEAM_ID
    return ticket_cmd.model_copy(update={"payload": payload})


def _map_transition_error(exc: Exception) -> str:
    name = type(exc).__name__
    mapping = {
        "InvalidTicketTransition": "invalid_transition",
        "TicketPermissionDenied": "permission_denied",
        "TicketVersionConflict": "version_conflict",
        "TicketNotFound": "ticket_not_found",
        "WorkflowOperationConflict": "workflow_operation_conflict",
    }
    return mapping.get(name, "execute_failed")
