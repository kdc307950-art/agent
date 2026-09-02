"""渠道受理共享辅助 —— HTTP 路由与异步 InboundWorker 共用同一份业务逻辑。

职责：intake 配置构造、命令序列化、受理结果命令、运营路由注入、intake 快照序列化。
Web 受理（/intake /resume）与渠道入站（wecom/dingtalk/内部通道）都从这里取实现，
避免异步化改造过程中产生行为分叉。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from langgraph.types import Command

from src.my_agent.helpdesk import (
    ActorType,
    PendingTicketInterrupt,
    ResumeAction,
    TicketAction,
    TicketCommand,
    TicketResumeCommand,
    TicketStatus,
    validate_resume_command,
)

_SLA_START_STATUSES = frozenset({TicketStatus.QUEUED, TicketStatus.ASSIGNED})


def intake_config(
    tenant_id: str,
    ticket_id: str,
    *,
    user_id: str | None = None,
    departments: tuple[str, ...] | list[str] | frozenset[str] = (),
    asset_id: str | None = None,
    internal: bool | None = None,
) -> dict[str, Any]:
    """构造受理图 config；身份上下文（user/departments/asset）仅来自服务端。

    调用方必须从认证主体或渠道入站事件填充 user_id/departments/asset_id；
    缺失时受理图默认收紧权限并转人工（见 graph.compose_answer_node）。
    """
    configurable: dict[str, Any] = {
        "thread_id": f"helpdesk:{tenant_id}:{ticket_id}",
        "tenant_id": tenant_id,
        "checkpoint_ns": "",
    }
    if user_id:
        configurable["user_id"] = user_id
    if departments:
        configurable["departments"] = [str(item) for item in departments if item]
    if asset_id:
        configurable["asset_id"] = asset_id
    if internal is not None:
        configurable["internal"] = internal
    return {"configurable": configurable}


def serialize_commands(commands: list[TicketCommand]) -> list[dict[str, Any]]:
    return [command.model_dump(mode="json") for command in commands]


def deserialize_commands(intent: dict[str, Any]) -> list[TicketCommand]:
    raw = intent.get("commands")
    if not isinstance(raw, list) or not raw:
        raise ValueError("工作流意图缺少 commands")
    return [TicketCommand.model_validate(item) for item in raw]


def classify_category(result: dict[str, Any]) -> str | None:
    """把分类结果拼接成工单 category（it + vpn -> it.vpn），与 tenant_it_policies 键一致。"""
    category = result.get("category")
    if not category:
        return None
    subcategory = result.get("subcategory")
    if subcategory and subcategory != "general":
        return f"{category}.{subcategory}"
    return str(category)


def intake_outcome_commands(
    *,
    ticket_id: str,
    actor_id: str,
    expected_version: int,
    result: dict[str, Any],
) -> list[TicketCommand]:
    """把受理图输出翻译成工单命令序列。

    有 __interrupt__（缺信息向客户追问）：只发 REQUEST_INFORMATION；
    否则发 CLASSIFY（分类）-> QUEUE（入队，带团队/优先级/原因码）两个连续命令，
    expected_version 依次递增以匹配乐观锁。
    """
    if "__interrupt__" in result:
        return [
            TicketCommand(
                ticket_id=ticket_id,
                action=TicketAction.REQUEST_INFORMATION,
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                expected_version=expected_version,
                payload={"missing_fields": result.get("missing_fields", [])},
            )
        ]
    return [
        TicketCommand(
            ticket_id=ticket_id,
            action=TicketAction.CLASSIFY,
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            expected_version=expected_version,
            payload={"category": classify_category(result)},
        ),
        TicketCommand(
            ticket_id=ticket_id,
            action=TicketAction.QUEUE,
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            expected_version=expected_version + 1,
            payload={
                "team_id": result.get("dispatch_team_id"),
                "priority": result.get("priority"),
                "reason_codes": result.get("dispatch_reason_codes", []),
            },
        ),
    ]


async def apply_operational_routing(
    runtime,
    commands: list[TicketCommand],
    result: dict[str, Any],
    *,
    tenant_id: str,
    channel: str,
) -> list[TicketCommand]:
    """按租户路由规则注入 QUEUE/ASSIGN 命令（无规则时保持原命令）。

    命中规则后把 QUEUE 命令的 team_id/reason_codes 替换为路由决策；
    若还选中了具体坐席，追加一条 ASSIGN 命令（期望版本接在最后一条命令后）。
    """
    if "__interrupt__" in result or not commands or not hasattr(runtime, "routing"):
        return commands
    decision = await runtime.routing.route(
        tenant_id=tenant_id,
        category=str(result.get("category") or "other"),
        subcategory=result.get("subcategory"),
        channel=channel,
        department_id=None,
        risk_level=str(result.get("risk_level") or "low"),
    )
    if decision.team_id is None:
        return commands
    updated = []
    for command in commands:
        if command.action == TicketAction.QUEUE:
            payload = dict(command.payload)
            payload.update(
                {"team_id": decision.team_id, "reason_codes": list(decision.reason_codes)}
            )
            command = command.model_copy(update={"payload": payload})
        updated.append(command)
    if decision.member_id is not None:
        updated.append(
            TicketCommand(
                ticket_id=commands[0].ticket_id,
                action=TicketAction.ASSIGN,
                actor_type=ActorType.SYSTEM,
                actor_id="routing-agent",
                expected_version=updated[-1].expected_version + 1,
                payload={
                    "team_id": decision.team_id,
                    "user_id": decision.member_id,
                    "reason_codes": list(decision.reason_codes),
                },
            )
        )
    return updated


def serialize_intake_result(ticket, result: dict[str, Any], snapshot: object) -> dict[str, Any]:
    """把受理结果 + 状态快照序列化为 API 响应。

    从快照的 interrupts 中提取第一个挂起项（interrupt_id + question/字段），
    missing_fields 取图状态中的值，供前端渲染「待补全」表单。
    """
    pending = None
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts = getattr(task, "interrupts", ()) or ()
        if interrupts:
            item = interrupts[0]
            value = getattr(item, "value", None)
            pending = {
                "interrupt_id": str(getattr(item, "id", "") or ""),
                **(value if isinstance(value, dict) else {"question": str(value)}),
            }
            state_values = getattr(snapshot, "values", None) or {}
            missing = state_values.get("missing_fields")
            if missing is not None and "missing_fields" not in pending:
                pending["missing_fields"] = list(missing)
            break
    return {
        "ticket": ticket,
        "state": {key: value for key, value in result.items() if key != "__interrupt__"},
        "interrupt": pending,
    }


async def pending_intake_interrupt(
    runtime, tenant_id: str, ticket_id: str
) -> dict[str, Any] | None:
    """查询工单当前是否挂起受理审批；无则返回 None。"""
    snapshot = await runtime.intake_graph.aget_state(intake_config(tenant_id, ticket_id))
    return serialize_intake_result(None, {}, snapshot)["interrupt"]


def encode_cursor(updated_at, ticket_id: str) -> str:
    """把 (updated_at, ticket_id) 编码为 URL-safe 游标（列表分页用）。"""
    raw = json.dumps([updated_at.isoformat(), ticket_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> tuple[Any, str] | None:
    """解码分页游标；None 返回 None，非法游标抛 ValueError（上层转 422）。"""
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        updated_at, ticket_id = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        parsed = __import__("datetime").datetime.fromisoformat(updated_at)
        if parsed.tzinfo is None or not isinstance(ticket_id, str) or not ticket_id:
            raise ValueError
        return parsed, ticket_id
    except Exception as exc:
        raise ValueError("无效分页游标") from exc


def pending_from_snapshot(snapshot: object, interrupt_id: str) -> PendingTicketInterrupt:
    """从 checkpoint 快照中取指定 interrupt 并构造 PendingTicketInterrupt。"""
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            if str(getattr(item, "id", "") or "") == interrupt_id:
                value = getattr(item, "value", None)
                if isinstance(value, dict):
                    return PendingTicketInterrupt(
                        interrupt_id=interrupt_id,
                        ticket_id=str(value.get("ticket_id") or ""),
                        expected_actor=ActorType(str(value.get("expected_actor") or "system")),
                        expected_actor_id=value.get("expected_actor_id"),
                        allowed_actions=frozenset(
                            ResumeAction(item) for item in (value.get("allowed_actions") or [])
                        ),
                    )
    raise ValueError("恢复标识已失效，请刷新后重试")


async def ensure_sla_for_ticket_if_needed(
    runtime,
    ticket,
    *,
    tenant_id: str,
    channel: str | None = None,
) -> bool:
    """为已进入排队/指派状态的工单幂等补建 SLA。

    状态流转和 SLA 使用不同仓储事务，SLA 插入可能在状态提交后暂时失败。
    所有幂等重试路径都调用本函数，确保后续重试能够修复这个可恢复缺口；
    ``ON CONFLICT DO NOTHING`` 使并发调用安全。
    """
    if ticket is None:
        return False
    try:
        status = TicketStatus(getattr(ticket, "status", ""))
    except (TypeError, ValueError):
        return False
    if status not in _SLA_START_STATUSES:
        return False
    return await runtime.ticket_operations.ensure_sla_for_ticket(
        tenant_id=tenant_id,
        ticket_id=ticket.ticket_id,
        channel=channel if channel is not None else getattr(ticket, "channel", None),
        category=getattr(ticket, "category", None),
    )


async def apply_intake_resume(
    runtime,
    *,
    tenant_id: str,
    ticket_id: str,
    interrupt_id: str,
    resume_command: TicketResumeCommand,
    operation_id: str,
    expected_version: int,
    scopes: set[str],
    channel: str,
    user_id: str | None = None,
    departments: tuple[str, ...] | list[str] | frozenset[str] = (),
    asset_id: str | None = None,
    internal: bool | None = None,
) -> dict[str, Any]:
    """执行一次受理恢复（Web 与企微回复共用）：

    校验挂起 interrupt → 图 resume → 生成命令 → 运营派单 → transition → SLA。
    身份上下文从认证主体/渠道事件传入；缺失时受理图收紧权限并转人工。
    """
    config = intake_config(
        tenant_id,
        ticket_id,
        user_id=user_id,
        departments=departments,
        asset_id=asset_id,
        internal=internal,
    )
    run = await runtime.tickets.start_workflow_operation(
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        operation_id=operation_id,
        command_type="resume",
        expected_version=expected_version,
        checkpoint_thread_id=config["configurable"]["thread_id"],
    )
    if run["status"] == "committed":
        snapshot = await runtime.intake_graph.aget_state(config)
        ticket = await runtime.tickets.get(tenant_id, ticket_id)
        await ensure_sla_for_ticket_if_needed(
            runtime, ticket, tenant_id=tenant_id, channel=channel
        )
        return {
            "ticket": ticket,
            "result": {},
            "interrupt": serialize_intake_result(None, {}, snapshot)["interrupt"],
        }

    snapshot = await runtime.intake_graph.aget_state(config)
    pending = pending_from_snapshot(snapshot, interrupt_id)
    if run["intent"] is not None:
        commands = deserialize_commands(run["intent"])
        result = dict(run["intent"].get("result") or {})
    else:
        validated = validate_resume_command(pending, resume_command, scopes=scopes)
        result = await runtime.intake_graph.ainvoke(
            Command(resume=validated.resume_payload), config
        )
        outcome = intake_outcome_commands(
            ticket_id=ticket_id,
            actor_id="intake-agent",
            expected_version=expected_version + 1,
            result=result,
        )
        commands = [validated.ticket_command, *outcome]
        commands = await apply_operational_routing(
            runtime, commands, result, tenant_id=tenant_id, channel=channel
        )
        await runtime.tickets.record_workflow_intent(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            operation_id=operation_id,
            intent={
                "commands": serialize_commands(commands),
                "result": {key: value for key, value in result.items() if key != "__interrupt__"},
            },
        )
    ticket = await runtime.tickets.transition_many(
        tenant_id,
        commands,
        scopes=scopes | {"ticket:system"},
        operation_id=operation_id,
    )
    await ensure_sla_for_ticket_if_needed(runtime, ticket, tenant_id=tenant_id, channel=channel)
    snapshot = await runtime.intake_graph.aget_state(config)
    return {
        "ticket": ticket,
        "result": result,
        "interrupt": serialize_intake_result(ticket, result, snapshot)["interrupt"],
    }
