"""渠道受理共享辅助 —— HTTP 路由与异步 InboundWorker 共用同一份业务逻辑。

职责：intake 配置构造、命令序列化、受理结果命令、运营路由注入、intake 快照序列化。
Web 受理（/intake /resume）与渠道入站（wecom/dingtalk/内部通道）都从这里取实现，
避免异步化改造过程中产生行为分叉。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from src.my_agent.helpdesk import ActorType, TicketAction, TicketCommand


def intake_config(tenant_id: str, ticket_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"helpdesk:{tenant_id}:{ticket_id}", "tenant_id": tenant_id, "checkpoint_ns": ""}}


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
    """按租户路由规则注入 QUEUE/ASSIGN 命令（无规则时保持原命令）。"""
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
            payload.update({"team_id": decision.team_id, "reason_codes": list(decision.reason_codes)})
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
                payload={"team_id": decision.team_id, "user_id": decision.member_id, "reason_codes": list(decision.reason_codes)},
            )
        )
    return updated


def serialize_intake_result(ticket, result: dict[str, Any], snapshot: object) -> dict[str, Any]:
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


async def pending_intake_interrupt(runtime, tenant_id: str, ticket_id: str) -> dict[str, Any] | None:
    snapshot = await runtime.intake_graph.aget_state(intake_config(tenant_id, ticket_id))
    return serialize_intake_result(None, {}, snapshot)["interrupt"]


def encode_cursor(updated_at, ticket_id: str) -> str:
    raw = json.dumps([updated_at.isoformat(), ticket_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> tuple[Any, str] | None:
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
