"""Helpdesk ticket and channel HTTP API."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from langgraph.types import Command

from src.my_agent.helpdesk import (
    ActorType,
    InvalidTicketTransition,
    PendingTicketInterrupt,
    TicketAction,
    TicketCommand,
    TicketPermissionDenied,
    TicketResumeCommand,
    TicketStatus,
    validate_resume_command,
)

from .channel_adapters import (
    DingTalkWebhookAdapter,
    NormalizedChannelEvent,
    WeComWebhookAdapter,
    WebhookVerificationError,
)
from .security import Principal, enforce_rate_limit, rate_limit_dependency
from .tickets import (
    AssetBindingError,
    CreateTicket,
    InboundEventConflict,
    ItPolicyNotFound,
    TicketAlreadyExists,
    TicketCapacityExceeded,
    TicketNotFound,
    TicketVersionConflict,
    UpsertItPolicy,
)


router = APIRouter(prefix="/tickets", tags=["tickets"])
channel_router = APIRouter(prefix="/integrations", tags=["integrations"])
admin_router = APIRouter(prefix="/admin/it", tags=["admin-it"])


class BindAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


class CreateTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    channel: str = Field(default="web", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    external_ticket_id: str | None = Field(default=None, min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=8_000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    asset_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class TransitionTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: TicketAction
    expected_version: int = Field(ge=0)
    actor_type: ActorType
    payload: dict[str, Any] = Field(default_factory=dict)


class StartIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=8_000)
    fields: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=0)


class ResumeIntakeRequest(TicketResumeCommand):
    operation_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class CreateSurveyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in_days: int = Field(default=7, ge=1, le=90)


class ReplayOutboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)


class SurveyResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=5)
    feedback: str | None = Field(default=None, max_length=4_000)


class InboundChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_event_id: str = Field(min_length=1, max_length=256)
    external_ticket_id: str | None = Field(default=None, min_length=1, max_length=256)
    requester_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=8_000)
    payload: dict[str, Any] = Field(default_factory=dict)


def _runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not hasattr(runtime, "tickets"):
        raise HTTPException(status_code=503, detail="工单服务尚未初始化")
    return runtime


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"缺少 {scope} 权限")


def _actor_scope(actor_type: ActorType) -> str:
    return {
        ActorType.CUSTOMER: "ticket:customer",
        ActorType.AGENT: "ticket:agent",
        ActorType.APPROVER: "ticket:approve",
        ActorType.SYSTEM: "ticket:system",
    }[actor_type]


def _encode_cursor(updated_at: datetime, ticket_id: str) -> str:
    raw = json.dumps([updated_at.isoformat(), ticket_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        updated_at, ticket_id = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        parsed = datetime.fromisoformat(updated_at)
        if parsed.tzinfo is None or not isinstance(ticket_id, str) or not ticket_id:
            raise ValueError
        return parsed, ticket_id
    except Exception as exc:
        raise HTTPException(status_code=422, detail="无效分页游标") from exc


def _intake_config(tenant_id: str, ticket_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"helpdesk:{tenant_id}:{ticket_id}", "tenant_id": tenant_id, "checkpoint_ns": ""}}


def _serialize_commands(commands: list[TicketCommand]) -> list[dict[str, Any]]:
    return [command.model_dump(mode="json") for command in commands]


def _deserialize_commands(intent: dict[str, Any]) -> list[TicketCommand]:
    raw = intent.get("commands")
    if not isinstance(raw, list) or not raw:
        raise ValueError("工作流意图缺少 commands")
    return [TicketCommand.model_validate(item) for item in raw]


def _intake_outcome_commands(
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
            payload={"category": result.get("category")},
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


async def _apply_operational_routing(
    runtime,
    commands: list[TicketCommand],
    result: dict[str, Any],
    *,
    tenant_id: str,
    channel: str,
) -> list[TicketCommand]:
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


def _serialize_intake_result(ticket, result: dict[str, Any], snapshot: object) -> dict[str, Any]:
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
            # 追问必须带缺失字段，前端才能渲染补全表单；从 checkpoint 状态补齐。
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


async def _pending_intake_interrupt(runtime, tenant_id: str, ticket_id: str) -> dict[str, Any] | None:
    snapshot = await runtime.intake_graph.aget_state(_intake_config(tenant_id, ticket_id))
    return _serialize_intake_result(None, {}, snapshot)["interrupt"]


def _interrupt_payload(snapshot: object, interrupt_id: str) -> tuple[object, dict[str, Any]]:
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            if str(getattr(item, "id", "") or "") == interrupt_id:
                value = getattr(item, "value", None)
                if isinstance(value, dict):
                    return item, value
    raise HTTPException(status_code=409, detail="恢复标识已失效，请刷新后重试")


async def _webhook_body(request: Request, channel: str) -> bytes:
    source_ip = request.client.host if request.client is not None else "unknown"
    await enforce_rate_limit(request, f"webhook:{channel}:{source_ip}", f"webhook:{channel}")
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > 256 * 1024:
        raise HTTPException(status_code=413, detail="Webhook 请求体超过 256 KB")
    body = await request.body()
    if len(body) > 256 * 1024:
        raise HTTPException(status_code=413, detail="Webhook 请求体超过 256 KB")
    return body


async def _persist_channel_event(runtime, event: NormalizedChannelEvent, *, actor_id: str):
    ticket_id = uuid4().hex
    created, ticket = await runtime.tickets.create_from_inbound_event(
        event.tenant_id,
        event.channel,
        event.external_event_id,
        event.payload,
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
    if not created or ticket is None:
        return {"created": False, "ticket_id": None if ticket is None else ticket.ticket_id, "ticket": ticket, "intake": None}
    operation_id = f"channel:{event.external_event_id}"
    config = _intake_config(event.tenant_id, ticket_id)
    run = await runtime.tickets.start_workflow_operation(
        tenant_id=event.tenant_id,
        ticket_id=ticket_id,
        operation_id=operation_id,
        command_type="channel_intake",
        expected_version=ticket.version,
        checkpoint_thread_id=config["configurable"]["thread_id"],
    )
    if run["status"] == "committed":
        snapshot = await runtime.intake_graph.aget_state(config)
        return {"created": True, "ticket_id": ticket_id, "ticket": ticket, "intake": {"ticket": ticket, "state": {}, "interrupt": _serialize_intake_result(None, {}, snapshot)["interrupt"]}}
    if run["intent"] is None:
        result = await runtime.intake_graph.ainvoke({
            "ticket_id": ticket_id,
            "requester_id": event.requester_id,
            "text": event.content,
            "fields": {"title": event.title, "description": event.content, "requester_id": event.requester_id},
            "clarification_rounds": 0,
        }, config)
        prefix = [TicketCommand(ticket_id=ticket_id, action=TicketAction.START_INTAKE, actor_type=ActorType.SYSTEM, actor_id="intake-agent", expected_version=ticket.version)]
        commands = [*prefix, *_intake_outcome_commands(ticket_id=ticket_id, actor_id="intake-agent", expected_version=ticket.version + 1, result=result)]
        commands = await _apply_operational_routing(runtime, commands, result, tenant_id=event.tenant_id, channel=event.channel)
        await runtime.tickets.record_workflow_intent(
            tenant_id=event.tenant_id,
            ticket_id=ticket_id,
            operation_id=operation_id,
            intent={"commands": _serialize_commands(commands), "result": {key: value for key, value in result.items() if key != "__interrupt__"}},
        )
    else:
        commands = _deserialize_commands(run["intent"])
        result = dict(run["intent"].get("result") or {})
    clarification = "、".join(result.get("missing_fields") or []) if "__interrupt__" in result else None
    if clarification:
        await runtime.ticket_operations.append_outbound_message(
            tenant_id=event.tenant_id,
            ticket_id=ticket_id,
            message_id=f"clarify-{event.external_event_id}",
            actor_type="system",
            actor_id="intake-agent",
            channel=event.channel,
            content=f"请补充：{clarification}",
            event_id=f"clarify-{event.external_event_id}",
            idempotency_key=f"clarify:{ticket_id}",
            payload={"ticket_id": ticket_id, "content": f"请补充：{clarification}"},
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
    return {"created": True, "ticket_id": ticket.ticket_id, "ticket": ticket, "intake": _serialize_intake_result(ticket, result, snapshot)}


def _map_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TicketNotFound):
        return HTTPException(status_code=404, detail="工单不存在")
    if isinstance(exc, TicketPermissionDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (TicketAlreadyExists, TicketVersionConflict, TicketCapacityExceeded, InboundEventConflict, InvalidTicketTransition)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="工单服务内部错误")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: CreateTicketRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:customer")
    runtime = _runtime(request)
    ticket_id = payload.ticket_id or uuid4().hex
    try:
        return await runtime.tickets.create(
            principal.tenant_id,
            CreateTicket(
                ticket_id=ticket_id,
                requester_id=principal.user_id,
                channel=payload.channel,
                external_ticket_id=payload.external_ticket_id,
                title=payload.title,
                description=payload.description,
                priority=payload.priority,
                actor_type=ActorType.CUSTOMER,
                actor_id=principal.user_id,
                asset_id=payload.asset_id,
                metadata=payload.metadata,
            ),
        )
    except Exception as exc:
        raise _map_domain_error(exc) from exc


@router.get("")
async def list_tickets_api(
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
    statuses: list[TicketStatus] = Query(default=[], alias="status"),
    category: str | None = Query(default=None, max_length=64),
    assigned_team_id: str | None = Query(default=None, max_length=128),
    assigned_user_id: str | None = Query(default=None, max_length=128),
    priority: Literal["low", "normal", "high", "urgent"] | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=128),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
):
    if not ({"ticket:customer", "ticket:agent"} & principal.scopes):
        raise HTTPException(status_code=403, detail="缺少工单读取权限")
    runtime = _runtime(request)
    is_agent = "ticket:agent" in principal.scopes
    items = await runtime.tickets.list_tickets(
        principal.tenant_id,
        requester_id=None if is_agent else principal.user_id,
        statuses=tuple(statuses),
        category=category,
        assigned_team_id=assigned_team_id if is_agent else None,
        assigned_user_id=(principal.user_id if assigned_user_id == "current_user" else assigned_user_id) if is_agent else None,
        priority=priority if is_agent else None,
        query_text=q,
        updated_before=_decode_cursor(cursor),
        limit=limit + 1,
    )
    has_more = len(items) > limit
    page = items[:limit]
    next_cursor = None
    if has_more and page:
        next_cursor = _encode_cursor(page[-1].updated_at, page[-1].ticket_id)
    return {"items": page, "next_cursor": next_cursor}


@router.get("/{ticket_id}")
async def get_ticket(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    if not ({"ticket:customer", "ticket:agent"} & principal.scopes):
        raise HTTPException(status_code=403, detail="缺少工单读取权限")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if "ticket:agent" not in principal.scopes and ticket.requester_id != principal.user_id:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ticket


@router.get("/{ticket_id}/overview")
async def get_ticket_overview(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    if not ({"ticket:customer", "ticket:agent"} & principal.scopes):
        raise HTTPException(status_code=403, detail="缺少工单读取权限")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or ("ticket:agent" not in principal.scopes and ticket.requester_id != principal.user_id):
        raise HTTPException(status_code=404, detail="工单不存在")
    overview = await runtime.ticket_operations.get_ticket_overview(principal.tenant_id, ticket_id)
    overview["status_events"] = await runtime.tickets.list_status_events(principal.tenant_id, ticket_id)
    return overview


@router.get("/{ticket_id}/pending-interrupt")
async def get_pending_ticket_interrupt(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    if not ({"ticket:customer", "ticket:agent"} & principal.scopes):
        raise HTTPException(status_code=403, detail="缺少工单读取权限")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or ("ticket:agent" not in principal.scopes and ticket.requester_id != principal.user_id):
        raise HTTPException(status_code=404, detail="工单不存在")
    return {"interrupt": await _pending_intake_interrupt(runtime, principal.tenant_id, ticket_id)}


@router.post("/{ticket_id}/intake")
async def start_ticket_intake(
    ticket_id: str,
    payload: StartIntakeRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:customer")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or ticket.requester_id != principal.user_id:
        raise HTTPException(status_code=404, detail="工单不存在")
    if payload.expected_version != ticket.version:
        existing = await runtime.tickets.get_workflow_operation(
            tenant_id=principal.tenant_id, ticket_id=ticket_id, operation_id=payload.operation_id
        )
        if existing is not None and existing["status"] == "committed":
            return {"ticket": ticket, "state": {}, "interrupt": await _pending_intake_interrupt(runtime, principal.tenant_id, ticket_id)}
        raise HTTPException(status_code=409, detail="工单版本已变化，请刷新后重试")
    try:
        run = await runtime.tickets.start_workflow_operation(
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            operation_id=payload.operation_id,
            command_type="intake",
            expected_version=ticket.version,
            checkpoint_thread_id=_intake_config(principal.tenant_id, ticket_id)["configurable"]["thread_id"],
        )
        if run["status"] == "committed":
            return {"ticket": ticket, "state": {}, "interrupt": await _pending_intake_interrupt(runtime, principal.tenant_id, ticket_id)}
        if run["intent"] is not None:
            commands = _deserialize_commands(run["intent"])
            result = dict(run["intent"].get("result") or {})
        else:
            if ticket.status not in {TicketStatus.NEW, TicketStatus.INTAKING}:
                raise InvalidTicketTransition(f"状态 {ticket.status} 不能启动受理")
            config = _intake_config(principal.tenant_id, ticket_id)
            result = await runtime.intake_graph.ainvoke(
                {"ticket_id": ticket_id, "requester_id": principal.user_id, "text": payload.text, "fields": payload.fields, "clarification_rounds": 0},
                config,
            )
            prefix = [] if ticket.status == TicketStatus.INTAKING else [TicketCommand(ticket_id=ticket_id, action=TicketAction.START_INTAKE, actor_type=ActorType.SYSTEM, actor_id="intake-agent", expected_version=ticket.version)]
            next_version = ticket.version + len(prefix)
            commands = [*prefix, *_intake_outcome_commands(ticket_id=ticket_id, actor_id="intake-agent", expected_version=next_version, result=result)]
            commands = await _apply_operational_routing(runtime, commands, result, tenant_id=principal.tenant_id, channel=getattr(ticket, "channel", "web"))
            await runtime.tickets.record_workflow_intent(tenant_id=principal.tenant_id, ticket_id=ticket_id, operation_id=payload.operation_id, intent={"commands": _serialize_commands(commands), "result": {key: value for key, value in result.items() if key != "__interrupt__"}})
        ticket = await runtime.tickets.transition_many(principal.tenant_id, commands, scopes={"ticket:system"}, operation_id=payload.operation_id)
        if ticket.status in {TicketStatus.QUEUED, TicketStatus.ASSIGNED}:
            await runtime.ticket_operations.ensure_sla_for_ticket(
                tenant_id=principal.tenant_id,
                ticket_id=ticket.ticket_id,
                channel=getattr(ticket, "channel", "web"),
                category=getattr(ticket, "category", None),
            )
        snapshot = await runtime.intake_graph.aget_state(_intake_config(principal.tenant_id, ticket_id))
        return _serialize_intake_result(ticket, result, snapshot)
    except Exception as exc:
        raise _map_domain_error(exc) from exc


@router.post("/{ticket_id}/resume")
async def resume_ticket_intake(
    ticket_id: str,
    payload: ResumeIntakeRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:customer")
    if payload.ticket_id != ticket_id:
        raise HTTPException(status_code=409, detail="恢复命令不属于当前工单")
    if payload.actor_type != ActorType.CUSTOMER:
        raise HTTPException(status_code=403, detail="客户恢复端点只允许 customer actor")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or ticket.requester_id != principal.user_id:
        raise HTTPException(status_code=404, detail="工单不存在")
    if payload.expected_version != ticket.version:
        existing = await runtime.tickets.get_workflow_operation(
            tenant_id=principal.tenant_id, ticket_id=ticket_id, operation_id=payload.operation_id
        )
        if existing is not None and existing["status"] == "committed":
            return {"ticket": ticket, "state": {}, "interrupt": await _pending_intake_interrupt(runtime, principal.tenant_id, ticket_id)}
        raise HTTPException(status_code=409, detail="工单版本已变化，请刷新后重试")
    config = _intake_config(principal.tenant_id, ticket_id)
    run = await runtime.tickets.start_workflow_operation(
        tenant_id=principal.tenant_id,
        ticket_id=ticket_id,
        operation_id=payload.operation_id,
        command_type="resume",
        expected_version=ticket.version,
        checkpoint_thread_id=config["configurable"]["thread_id"],
    )
    if run["status"] == "committed":
        return {"ticket": ticket, "state": {}, "interrupt": await _pending_intake_interrupt(runtime, principal.tenant_id, ticket_id)}
    snapshot = await runtime.intake_graph.aget_state(config)
    _item, value = _interrupt_payload(snapshot, payload.interrupt_id)
    try:
        pending = PendingTicketInterrupt(
            interrupt_id=payload.interrupt_id,
            ticket_id=str(value.get("ticket_id") or ""),
            expected_actor=ActorType(value.get("expected_actor")),
            expected_actor_id=value.get("expected_actor_id"),
            allowed_actions=value.get("allowed_actions") or [],
        )
        if run["intent"] is not None:
            commands = _deserialize_commands(run["intent"])
            result = dict(run["intent"].get("result") or {})
        else:
            server_command = payload.model_copy(update={"actor_id": principal.user_id})
            validated = validate_resume_command(pending, server_command, scopes=principal.scopes)
            result = await runtime.intake_graph.ainvoke(Command(resume=validated.resume_payload), config)
            outcome = _intake_outcome_commands(ticket_id=ticket_id, actor_id="intake-agent", expected_version=ticket.version + 1, result=result)
            commands = [validated.ticket_command, *outcome]
            commands = await _apply_operational_routing(runtime, commands, result, tenant_id=principal.tenant_id, channel=getattr(ticket, "channel", "web"))
            await runtime.tickets.record_workflow_intent(tenant_id=principal.tenant_id, ticket_id=ticket_id, operation_id=payload.operation_id, intent={"commands": _serialize_commands(commands), "result": {key: value for key, value in result.items() if key != "__interrupt__"}})
        ticket = await runtime.tickets.transition_many(
            principal.tenant_id,
            commands,
            scopes=principal.scopes | {"ticket:system"},
            operation_id=payload.operation_id,
        )
        if ticket.status in {TicketStatus.QUEUED, TicketStatus.ASSIGNED}:
            await runtime.ticket_operations.ensure_sla_for_ticket(
                tenant_id=principal.tenant_id,
                ticket_id=ticket.ticket_id,
                channel=getattr(ticket, "channel", "web"),
                category=getattr(ticket, "category", None),
            )
        snapshot = await runtime.intake_graph.aget_state(config)
        return _serialize_intake_result(ticket, result, snapshot)
    except (ValueError, TicketPermissionDenied, TicketVersionConflict, InvalidTicketTransition) as exc:
        if isinstance(exc, TicketPermissionDenied):
            code = 403
        else:
            code = 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.post("/{ticket_id}/survey", status_code=status.HTTP_201_CREATED)
async def create_ticket_survey(
    ticket_id: str,
    payload: CreateSurveyRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    if await runtime.tickets.get(principal.tenant_id, ticket_id) is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    survey_id = uuid4().hex
    created = await runtime.ticket_operations.create_survey(
        tenant_id=principal.tenant_id,
        ticket_id=ticket_id,
        survey_id=survey_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days),
        outbox_event_id=f"survey-{survey_id}",
    )
    if not created:
        raise HTTPException(status_code=409, detail="该工单已有回访")
    return {"survey_id": survey_id, "status": "pending"}


@router.post("/{ticket_id}/survey/{survey_id}/response")
async def respond_ticket_survey(
    ticket_id: str,
    survey_id: str,
    payload: SurveyResponseRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:customer")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or ticket.requester_id != principal.user_id:
        raise HTTPException(status_code=404, detail="工单不存在")
    updated = await runtime.ticket_operations.respond_survey(
        tenant_id=principal.tenant_id,
        survey_id=survey_id,
        score=payload.score,
        feedback=payload.feedback,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="回访不存在、已提交或已过期")
    return {"status": "responded"}


@router.post("/{ticket_id}/transitions")
async def transition_ticket_api(
    ticket_id: str,
    payload: TransitionTicketRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    scope = _actor_scope(payload.actor_type)
    _require_scope(principal, scope)
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if payload.actor_type == ActorType.CUSTOMER and ticket.requester_id != principal.user_id:
        raise HTTPException(status_code=404, detail="工单不存在")
    try:
        await runtime.ticket_operations.ensure_sla_for_ticket(
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            channel=getattr(ticket, "channel", None),
            category=getattr(ticket, "category", None),
        )
        updated = await runtime.tickets.transition(
            principal.tenant_id,
            TicketCommand(
                ticket_id=ticket_id,
                action=payload.action,
                actor_type=payload.actor_type,
                actor_id=principal.user_id,
                expected_version=payload.expected_version,
                payload=payload.payload,
            ),
            scopes=principal.scopes,
        )
        if updated.status == TicketStatus.AWAITING_CUSTOMER:
            await runtime.ticket_operations.pause_sla(principal.tenant_id, ticket_id, reason="awaiting_customer")
        elif updated.status in {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED}:
            await runtime.ticket_operations.resume_sla(principal.tenant_id, ticket_id, resumed_at=datetime.now(timezone.utc))
            await runtime.ticket_operations.mark_first_response(principal.tenant_id, ticket_id)
        return updated
    except Exception as exc:
        raise _map_domain_error(exc) from exc


@channel_router.get("/outbox/dead")
async def list_dead_outbox_events(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:agent")
    return {"items": await _runtime(request).ticket_operations.list_dead_outbox(tenant_id=principal.tenant_id, limit=limit)}


@channel_router.post("/outbox/replay")
async def replay_dead_outbox_event(
    payload: ReplayOutboxRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:agent")
    replayed = await _runtime(request).ticket_operations.replay_dead_outbox(principal.tenant_id, payload.event_id)
    if not replayed:
        raise HTTPException(status_code=404, detail="死信事件不存在")
    return {"event_id": payload.event_id, "status": "pending"}


@channel_router.post("/{channel}/events")
async def receive_channel_event(
    channel: str,
    payload: InboundChannelRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:channel")
    runtime = _runtime(request)
    try:
        return await _persist_channel_event(
            runtime,
            NormalizedChannelEvent(
                tenant_id=principal.tenant_id,
                channel=channel,
                external_event_id=payload.external_event_id,
                external_ticket_id=payload.external_ticket_id,
                requester_id=payload.requester_id,
                title=payload.title,
                content=payload.content,
                payload=payload.payload,
            ),
            actor_id=principal.user_id,
        )
    except Exception as exc:
        raise _map_domain_error(exc) from exc


@channel_router.post("/wecom/webhook")
async def receive_wecom_webhook(
    request: Request,
    timestamp: str = Query(min_length=1, max_length=20),
    nonce: str = Query(min_length=1, max_length=128),
    msg_signature: str = Query(min_length=1, max_length=128),
):
    settings = request.app.state.settings
    if not all((settings.wecom_tenant_id, settings.wecom_token, settings.wecom_encoding_aes_key, settings.wecom_corp_id)):
        raise HTTPException(status_code=503, detail="企业微信 Webhook 未配置")
    adapter = WeComWebhookAdapter(
        tenant_id=settings.wecom_tenant_id,
        token=settings.wecom_token,
        encoding_aes_key=settings.wecom_encoding_aes_key,
        corp_id=settings.wecom_corp_id,
        replay_window_seconds=settings.webhook_replay_window_seconds,
    )
    body = await _webhook_body(request, "wecom")
    try:
        event = adapter.verify_and_parse(
            body,
            timestamp=timestamp,
            nonce=nonce,
            signature=msg_signature,
        )
        return await _persist_channel_event(_runtime(request), event, actor_id="wecom-webhook")
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise _map_domain_error(exc) from exc


@channel_router.post("/dingtalk/webhook")
async def receive_dingtalk_webhook(
    request: Request,
    timestamp: str = Query(min_length=1, max_length=20),
    sign: str = Query(min_length=1, max_length=512),
):
    settings = request.app.state.settings
    if not settings.dingtalk_tenant_id or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=503, detail="钉钉 Webhook 未配置")
    adapter = DingTalkWebhookAdapter(
        tenant_id=settings.dingtalk_tenant_id,
        app_secret=settings.dingtalk_app_secret,
        replay_window_seconds=settings.webhook_replay_window_seconds,
    )
    body = await _webhook_body(request, "dingtalk")
    try:
        event = adapter.verify_and_parse(
            body,
            timestamp=timestamp,
            signature=sign,
        )
        return await _persist_channel_event(_runtime(request), event, actor_id="dingtalk-webhook")
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise _map_domain_error(exc) from exc


# ========== IT 策略管理（/admin/it/policies） ==========

@admin_router.get("/policies")
async def list_it_policies(
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "it-policy:read")
    runtime = _runtime(request)
    items = await runtime.it_policies.list_active(principal.tenant_id)
    return {"items": items}


@admin_router.delete("/policies/{category}")
async def delete_it_policy(
    category: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "it-policy:write")
    runtime = _runtime(request)
    deleted = await runtime.it_policies.delete(principal.tenant_id, category)
    if not deleted:
        raise HTTPException(status_code=404, detail="策略不存在")
    audit = getattr(runtime, "audit", None)
    if audit is not None:
        await audit.record_admin_event(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            action="it_policy.delete",
            resource_type="it_policy",
            resource_id=category,
        )
    return {"category": category, "deleted": True}


@admin_router.get("/policies/{category}")
async def get_it_policy(
    category: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "it-policy:read")
    runtime = _runtime(request)
    policy = await runtime.it_policies.get(principal.tenant_id, category)
    if policy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return policy


@admin_router.put("/policies/{category}")
async def upsert_it_policy(
    category: str,
    payload: UpsertItPolicy,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "it-policy:write")
    runtime = _runtime(request)
    if payload.category != category:
        raise HTTPException(status_code=409, detail="路径与请求体中的 category 不一致")
    try:
        result = await runtime.it_policies.upsert(principal.tenant_id, payload)
    except ItPolicyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit = getattr(runtime, "audit", None)
    if audit is not None:
        await audit.record_admin_event(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            action="it_policy.upsert",
            resource_type="it_policy",
            resource_id=category,
            detail={"policy_id": payload.policy_id},
        )
    return result


# ========== 工单资产绑定（POST/DELETE /tickets/{ticket_id}/asset） ==========

@router.post("/{ticket_id}/asset")
async def bind_ticket_asset(
    ticket_id: str,
    payload: BindAssetRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    try:
        return await runtime.tickets.bind_asset(principal.tenant_id, ticket_id, payload.asset_id)
    except AssetBindingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{ticket_id}/asset")
async def unbind_ticket_asset(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    try:
        return await runtime.tickets.unbind_asset(principal.tenant_id, ticket_id)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
