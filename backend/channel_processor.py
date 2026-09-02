"""渠道入站事件异步处理核心 —— InboundWorker 调用，HTTP 路由不执行。

业务链：企微回复优先匹配待补全工单并恢复受理（绝不新建工单）；否则幂等建单
→ 受理图 → 分类/追问 → 运营派单 → transition → SLA → 澄清 Outbox。
异步化只改变「谁在什么时候调用」，业务逻辑唯一一份。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.my_agent.helpdesk import (
    ActorType,
    ResumeAction,
    TicketAction,
    TicketCommand,
    TicketResumeCommand,
    TicketStatus,
)

from .channel_adapters import NormalizedChannelEvent
from .ticket_intake import (
    apply_intake_resume,
    apply_operational_routing,
    deserialize_commands,
    ensure_sla_for_ticket_if_needed,
    intake_config,
    intake_outcome_commands,
    serialize_commands,
    serialize_intake_result,
)
from .tickets import CreateTicket

_PENDING_EXPIRY_DAYS = 7
_FIELD_RE = re.compile(r"^([A-Za-z_\u4e00-\u9fa5]+)\s*[:：=]\s*(.+)$")


def _parse_field_reply(content: str) -> dict[str, str]:
    """解析「字段:值 / 字段：值 / 字段=值」键值对回复；无法解析的返回空 dict。"""
    fields: dict[str, str] = {}
    for line in content.splitlines():
        match = _FIELD_RE.match(line.strip())
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


def _first_interrupt_id(snapshot: object) -> str | None:
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            return str(getattr(item, "id", "") or "")
    return None


async def _event_identity(
    runtime, event: NormalizedChannelEvent
) -> tuple[str, list[str], str | None, bool, bool]:
    """从可信渠道身份目录读取身份上下文（Day 3-4 加固）。

    - 完全忽略请求体/事件 payload 中的 departments / asset_id / internal（防止伪造）；
    - 只读 channel_identities 目录（由管理员或专用 Webhook 验签后写入）；
    - 无映射或失效 -> 空部门 + 空资产 + identity_missing=True（受理图转人工）；
    - 返回 (requester_id, departments, asset_id, identity_missing, internal)。
    """
    repo = getattr(runtime, "channel_identities", None)
    identity = None
    if repo is not None:
        try:
            identity = await repo.get(event.tenant_id, event.channel, event.requester_id)
        except Exception:
            identity = None
    if identity is None or not identity.active:
        return event.requester_id, [], None, True, False
    return (
        event.requester_id,
        list(identity.departments),
        identity.asset_id,
        False,
        bool(identity.internal),
    )


async def _resume_from_customer_reply(
    runtime, event: NormalizedChannelEvent, pending: dict, *, actor_id: str
) -> dict | None:
    """客户企微回复：关联唯一待补全工单恢复受理；不满足条件返回 None（走普通新工单）。"""
    tenant_id = pending["tenant_id"]
    ticket_id = pending["ticket_id"]
    ticket = await runtime.tickets.get(tenant_id, ticket_id)
    if ticket is None or ticket.status != TicketStatus.AWAITING_CUSTOMER:
        # 已关闭/已取消/非等待补充：不得 resume，按普通新消息处理。
        return None

    fields = _parse_field_reply(event.content)
    if not fields:
        await runtime.ticket_operations.append_outbound_message(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            message_id=f"clarify-format-{event.external_event_id}",
            actor_type="system",
            actor_id="intake-agent",
            channel=event.channel,
            content="请按「字段:值」格式补充，例如：device: laptop-001，operating_system: Windows 11",
            event_id=f"clarify-format-{event.external_event_id}",
            idempotency_key=f"clarify-format:{ticket_id}",
            payload={
                "ticket_id": ticket_id,
                "content": "请按「字段:值」格式补充",
                "channel": event.channel,
            },
        )
        return {
            "resumed": False,
            "reason": "unparsable_reply",
            "ticket_id": ticket_id,
            "ticket": ticket,
        }

    requester_id, departments, asset_id, _identity_missing, internal = await _event_identity(
        runtime, event
    )
    config = intake_config(
        tenant_id,
        ticket_id,
        user_id=requester_id,
        departments=departments,
        asset_id=asset_id,
        internal=internal,
    )
    snapshot = await runtime.intake_graph.aget_state(config)
    state_fields = dict((getattr(snapshot, "values", None) or {}).get("fields") or {})
    merged = {**state_fields, **fields}
    interrupt_id = _first_interrupt_id(snapshot)
    if interrupt_id is None:
        return None
    resume_command = TicketResumeCommand(
        interrupt_id=interrupt_id,
        ticket_id=ticket_id,
        actor_type=ActorType.CUSTOMER,
        actor_id=event.requester_id,
        action=ResumeAction.PROVIDE_INFORMATION,
        expected_version=ticket.version,
        payload={"fields": merged},
    )
    outcome = await apply_intake_resume(
        runtime,
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        interrupt_id=interrupt_id,
        resume_command=resume_command,
        operation_id=f"channel-resume:{event.external_event_id}",
        expected_version=ticket.version,
        scopes={"ticket:customer", "ticket:system"},
        channel=event.channel,
        user_id=requester_id,
        departments=departments,
        asset_id=asset_id,
        internal=internal,
    )
    await runtime.tickets.mark_pending_intake_resumed(tenant_id, ticket_id)
    # 状态流水在 transition 之后追加，避免与状态事件的 ticket_version 唯一约束冲突。
    await runtime.tickets.append_status_event(
        tenant_id,
        ticket_id,
        action="customer_reply_received",
        actor_type="customer",
        actor_id=event.requester_id,
        dedupe_key=f"inbound:{event.channel}:{event.external_event_id}:customer_reply",
    )
    await runtime.tickets.append_status_event(
        tenant_id,
        ticket_id,
        action="intake_resumed",
        actor_type="system",
        actor_id=actor_id,
        dedupe_key=f"inbound:{event.channel}:{event.external_event_id}:intake_resumed",
    )
    return {
        "resumed": True,
        "ticket_id": ticket_id,
        "ticket": outcome["ticket"],
        "intake": {"state": outcome["result"], "interrupt": outcome["interrupt"]},
    }


async def process_inbound_event(runtime, event: NormalizedChannelEvent, *, actor_id: str) -> dict:
    """处理一条已登记（received）的渠道入站事件。

    处理流程（业务链唯一一份，Worker 与 HTTP 共用）：
      1. 企微客户回复优先匹配待补全工单 → resume 原工单受理（绝不新建）；
      2. 否则建单：事件未关联工单则新建 ticket，已关联则复用上次崩溃遗留的工单恢复；
      3. 跑受理图（若未开始）或从记录的工作流意图续跑，得到 分类/追问/派单 命令；
      4. 若有缺字段 → 登记待补全关联并写入澄清 Outbox 消息；
      5. transition_many 执行命令，进入 Q/A 状态时补建 SLA；最后返回受理快照。
    """
    existing = await runtime.tickets.get_inbound_event(
        event.tenant_id, event.channel, event.external_event_id
    )
    if existing is None:
        raise ValueError("inbound 事件不存在")
    if existing["status"] not in ("processing",):
        raise ValueError(f"inbound 事件状态异常: {existing['status']}")

    # 企微回复：先按 (tenant, channel, external_user_id) 匹配唯一有效待补全工单。
    pending = await runtime.tickets.find_pending_intake(
        event.tenant_id, event.channel, event.requester_id
    )
    if pending is not None:
        resumed = await _resume_from_customer_reply(runtime, event, pending, actor_id=actor_id)
        if resumed is not None:
            return resumed

    # 幂等：事件已关联工单（上次崩溃在建单后/受理中）→ 复用该工单恢复受理，
    # 避免同 event_id 重复登记造成的重复建单；未关联则新建工单并回写关联。
    ticket = None
    if existing["ticket_id"] is not None:
        ticket = await runtime.tickets.get(event.tenant_id, existing["ticket_id"])
    if ticket is None:
        ticket_id = uuid4().hex
        ticket = await runtime.tickets.create(
            event.tenant_id,
            CreateTicket(
                ticket_id=ticket_id,
                requester_id=event.requester_id,
                channel=event.channel,
                external_ticket_id=event.external_ticket_id,
                title=event.title,
                description=event.content,
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                metadata={"external_event_id": event.external_event_id},
            ),
        )
        await runtime.tickets.attach_inbound_event(
            event.tenant_id, event.channel, event.external_event_id, ticket.ticket_id
        )
    created = existing["ticket_id"] is None

    operation_id = f"channel:{event.external_event_id}"
    requester_id, departments, asset_id, identity_missing, internal = await _event_identity(
        runtime, event
    )
    config = intake_config(
        event.tenant_id,
        ticket.ticket_id,
        user_id=requester_id,
        departments=departments,
        asset_id=asset_id,
        internal=internal,
    )
    run = await runtime.tickets.start_workflow_operation(
        tenant_id=event.tenant_id,
        ticket_id=ticket.ticket_id,
        operation_id=operation_id,
        command_type="channel_intake",
        expected_version=ticket.version,
        checkpoint_thread_id=config["configurable"]["thread_id"],
    )
    if run["status"] == "committed":
        # 受理图上一轮已提交（例如崩溃后恢复）：直接复用挂起状态，不再重复跑图。
        snapshot = await runtime.intake_graph.aget_state(config)
        # transition 与 SLA 不在同一事务；幂等重试时重新补建可能遗漏的 SLA。
        current_ticket = await runtime.tickets.get(event.tenant_id, ticket.ticket_id)
        if current_ticket is not None:
            ticket = current_ticket
        await ensure_sla_for_ticket_if_needed(
            runtime, ticket, tenant_id=event.tenant_id, channel=event.channel
        )
        return {
            "created": created,
            "ticket_id": ticket.ticket_id,
            "ticket": ticket,
            "intake": {
                "ticket": ticket,
                "state": {},
                "interrupt": serialize_intake_result(None, {}, snapshot)["interrupt"],
            },
        }
    if run["intent"] is None:
        result = await runtime.intake_graph.ainvoke(
            {
                "ticket_id": ticket.ticket_id,
                "requester_id": event.requester_id,
                "text": event.content,
                "fields": {
                    "title": event.title,
                    "description": event.content,
                    "requester_id": event.requester_id,
                },
                "clarification_rounds": 0,
                "channel_identity_missing": identity_missing,
            },
            config,
        )
        prefix = [
            TicketCommand(
                ticket_id=ticket.ticket_id,
                action=TicketAction.START_INTAKE,
                actor_type=ActorType.SYSTEM,
                actor_id="intake-agent",
                expected_version=ticket.version,
            )
        ]
        commands = [
            *prefix,
            *intake_outcome_commands(
                ticket_id=ticket.ticket_id,
                actor_id="intake-agent",
                expected_version=ticket.version + 1,
                result=result,
            ),
        ]
        commands = await apply_operational_routing(
            runtime, commands, result, tenant_id=event.tenant_id, channel=event.channel
        )
        await runtime.tickets.record_workflow_intent(
            tenant_id=event.tenant_id,
            ticket_id=ticket.ticket_id,
            operation_id=operation_id,
            intent={
                "commands": serialize_commands(commands),
                "result": {key: value for key, value in result.items() if key != "__interrupt__"},
            },
        )
    else:
        commands = deserialize_commands(run["intent"])
        result = dict(run["intent"].get("result") or {})

    # 受理图被 interrupt 挂起（缺字段）时，向客户登记待补全关联并推送澄清消息。
    clarification = (
        "、".join(result.get("missing_fields") or []) if "__interrupt__" in result else None
    )
    if clarification:
        expires_at = datetime.now(UTC) + timedelta(days=_PENDING_EXPIRY_DAYS)
        # 登记客户待补全关联（企微回复时可恢复原工单，绝不新建）。
        await runtime.tickets.register_pending_intake(
            tenant_id=event.tenant_id,
            ticket_id=ticket.ticket_id,
            channel=event.channel,
            external_user_id=event.requester_id,
            required_fields=list(result.get("missing_fields") or []),
            expires_at=expires_at,
        )
        local_expiry = expires_at.astimezone().strftime("%Y-%m-%d %H:%M")
        clarify_content = (
            f"工单 {ticket.ticket_id} 需补充：{clarification}\n"
            f"请在 {local_expiry} 前按「字段:值」格式回复（如 device: laptop-001），逾期将作废。"
        )
        await runtime.ticket_operations.append_outbound_message(
            tenant_id=event.tenant_id,
            ticket_id=ticket.ticket_id,
            message_id=f"clarify-{event.external_event_id}",
            actor_type="system",
            actor_id="intake-agent",
            channel=event.channel,
            content=clarify_content,
            event_id=f"clarify-{event.external_event_id}",
            idempotency_key=f"clarify:{ticket.ticket_id}",
            payload={
                "ticket_id": ticket.ticket_id,
                "content": clarify_content,
                "channel": event.channel,
            },
        )
    ticket = await runtime.tickets.transition_many(
        event.tenant_id,
        commands,
        scopes={"ticket:system"},
        operation_id=operation_id,
    )
    await ensure_sla_for_ticket_if_needed(
        runtime, ticket, tenant_id=event.tenant_id, channel=event.channel
    )
    snapshot = await runtime.intake_graph.aget_state(config)
    return {
        "created": created,
        "ticket_id": ticket.ticket_id,
        "ticket": ticket,
        "intake": serialize_intake_result(ticket, result, snapshot),
    }
