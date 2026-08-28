"""渠道入站事件异步处理核心 —— InboundWorker 调用，HTTP 路由不执行。

业务链：幂等建单 → 受理图 → 分类/追问 → 运营派单 → transition → SLA → 澄清 Outbox。
与原同步路径行为一致；异步化只改变「谁在什么时候调用」，业务逻辑唯一一份。
"""

from __future__ import annotations

from uuid import uuid4

from src.my_agent.helpdesk import ActorType, TicketAction, TicketCommand, TicketStatus

from .channel_adapters import NormalizedChannelEvent
from .ticket_intake import (
    apply_operational_routing,
    deserialize_commands,
    intake_config,
    intake_outcome_commands,
    serialize_commands,
    serialize_intake_result,
)
from .tickets import CreateTicket


async def process_inbound_event(runtime, event: NormalizedChannelEvent, *, actor_id: str) -> dict:
    """处理一条已登记（received）的渠道入站事件：建单 + 受理，返回结果 dict。"""
    existing = await runtime.tickets.get_inbound_event(
        event.tenant_id, event.channel, event.external_event_id
    )
    if existing is None:
        raise ValueError("inbound 事件不存在")
    if existing["status"] not in ("processing",):
        raise ValueError(f"inbound 事件状态异常: {existing['status']}")

    # 幂等：事件已关联工单（上次崩溃在建单后/受理中）→ 复用该工单恢复受理。
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
    config = intake_config(event.tenant_id, ticket.ticket_id)
    run = await runtime.tickets.start_workflow_operation(
        tenant_id=event.tenant_id,
        ticket_id=ticket.ticket_id,
        operation_id=operation_id,
        command_type="channel_intake",
        expected_version=ticket.version,
        checkpoint_thread_id=config["configurable"]["thread_id"],
    )
    if run["status"] == "committed":
        snapshot = await runtime.intake_graph.aget_state(config)
        return {
            "created": created,
            "ticket_id": ticket.ticket_id,
            "ticket": ticket,
            "intake": {"ticket": ticket, "state": {}, "interrupt": serialize_intake_result(None, {}, snapshot)["interrupt"]},
        }
    if run["intent"] is None:
        result = await runtime.intake_graph.ainvoke({
            "ticket_id": ticket.ticket_id,
            "requester_id": event.requester_id,
            "text": event.content,
            "fields": {"title": event.title, "description": event.content, "requester_id": event.requester_id},
            "clarification_rounds": 0,
        }, config)
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

    clarification = "、".join(result.get("missing_fields") or []) if "__interrupt__" in result else None
    if clarification:
        await runtime.ticket_operations.append_outbound_message(
            tenant_id=event.tenant_id,
            ticket_id=ticket.ticket_id,
            message_id=f"clarify-{event.external_event_id}",
            actor_type="system",
            actor_id="intake-agent",
            channel=event.channel,
            content=f"请补充：{clarification}",
            event_id=f"clarify-{event.external_event_id}",
            idempotency_key=f"clarify:{ticket.ticket_id}",
            payload={"ticket_id": ticket.ticket_id, "content": f"请补充：{clarification}"},
        )
    ticket = await runtime.tickets.transition_many(
        event.tenant_id,
        commands,
        scopes={"ticket:system"},
        operation_id=operation_id,
    )
    if ticket.status in {TicketStatus.QUEUED, TicketStatus.ASSIGNED}:
        await runtime.ticket_operations.ensure_sla_for_ticket(
            tenant_id=event.tenant_id,
            ticket_id=ticket.ticket_id,
            channel=event.channel,
            category=getattr(ticket, "category", None),
        )
    snapshot = await runtime.intake_graph.aget_state(config)
    return {
        "created": created,
        "ticket_id": ticket.ticket_id,
        "ticket": ticket,
        "intake": serialize_intake_result(ticket, result, snapshot),
    }
