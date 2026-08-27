"""Helpdesk ticket and channel HTTP API."""

from __future__ import annotations

import base64
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
from .security import Principal, authenticate
from .tickets import (
    CreateTicket,
    InboundEventConflict,
    TicketAlreadyExists,
    TicketNotFound,
    TicketVersionConflict,
)


router = APIRouter(prefix="/tickets", tags=["tickets"])
channel_router = APIRouter(prefix="/integrations", tags=["integrations"])


class CreateTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    channel: str = Field(default="web", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    external_ticket_id: str | None = Field(default=None, min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=8_000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TransitionTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: TicketAction
    expected_version: int = Field(ge=0)
    actor_type: ActorType
    payload: dict[str, Any] = Field(default_factory=dict)


class StartIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=8_000)
    fields: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=0)


class ResumeIntakeRequest(TicketResumeCommand):
    pass


class CreateSurveyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in_days: int = Field(default=7, ge=1, le=90)


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
    return {"configurable": {"thread_id": f"helpdesk:{tenant_id}:{ticket_id}", "checkpoint_ns": ""}}


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
            break
    return {
        "ticket": ticket,
        "state": {key: value for key, value in result.items() if key != "__interrupt__"},
        "interrupt": pending,
    }


def _interrupt_payload(snapshot: object, interrupt_id: str) -> tuple[object, dict[str, Any]]:
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            if str(getattr(item, "id", "") or "") == interrupt_id:
                value = getattr(item, "value", None)
                if isinstance(value, dict):
                    return item, value
    raise HTTPException(status_code=409, detail="恢复标识已失效，请刷新后重试")


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
    ticket = await runtime.tickets.transition(
        event.tenant_id,
        TicketCommand(ticket_id=ticket_id, action=TicketAction.START_INTAKE, actor_type=ActorType.SYSTEM, actor_id="intake-agent", expected_version=ticket.version),
        scopes={"ticket:system"},
    )
    config = _intake_config(event.tenant_id, ticket_id)
    result = await runtime.intake_graph.ainvoke({
        "ticket_id": ticket_id,
        "requester_id": event.requester_id,
        "text": event.content,
        "fields": {"title": event.title, "description": event.content, "requester_id": event.requester_id},
        "clarification_rounds": 0,
    }, config)
    ticket = await runtime.tickets.transition_many(
        event.tenant_id,
        _intake_outcome_commands(ticket_id=ticket_id, actor_id="intake-agent", expected_version=ticket.version, result=result),
        scopes={"ticket:system"},
    )
    snapshot = await runtime.intake_graph.aget_state(config)
    return {"created": True, "ticket_id": ticket.ticket_id, "ticket": ticket, "intake": _serialize_intake_result(ticket, result, snapshot)}


def _map_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TicketNotFound):
        return HTTPException(status_code=404, detail="工单不存在")
    if isinstance(exc, TicketPermissionDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (TicketAlreadyExists, TicketVersionConflict, InboundEventConflict, InvalidTicketTransition)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="工单服务内部错误")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: CreateTicketRequest,
    request: Request,
    principal: Principal = Depends(authenticate),
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
                metadata=payload.metadata,
            ),
        )
    except Exception as exc:
        raise _map_domain_error(exc) from exc


@router.get("")
async def list_tickets_api(
    request: Request,
    principal: Principal = Depends(authenticate),
    statuses: list[TicketStatus] = Query(default=[], alias="status"),
    category: str | None = Query(default=None, max_length=64),
    assigned_team_id: str | None = Query(default=None, max_length=128),
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
    principal: Principal = Depends(authenticate),
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


@router.post("/{ticket_id}/intake")
async def start_ticket_intake(
    ticket_id: str,
    payload: StartIntakeRequest,
    request: Request,
    principal: Principal = Depends(authenticate),
):
    _require_scope(principal, "ticket:customer")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or ticket.requester_id != principal.user_id:
        raise HTTPException(status_code=404, detail="工单不存在")
    if payload.expected_version != ticket.version:
        raise HTTPException(status_code=409, detail="工单版本已变化，请刷新后重试")
    try:
        if ticket.status == TicketStatus.NEW:
            ticket = await runtime.tickets.transition(
                principal.tenant_id,
                TicketCommand(
                    ticket_id=ticket_id,
                    action=TicketAction.START_INTAKE,
                    actor_type=ActorType.SYSTEM,
                    actor_id="intake-agent",
                    expected_version=ticket.version,
                ),
                scopes={"ticket:system"},
            )
        elif ticket.status != TicketStatus.INTAKING:
            raise InvalidTicketTransition(f"状态 {ticket.status} 不能启动受理")
        config = _intake_config(principal.tenant_id, ticket_id)
        result = await runtime.intake_graph.ainvoke(
            {
                "ticket_id": ticket_id,
                "requester_id": principal.user_id,
                "text": payload.text,
                "fields": payload.fields,
                "clarification_rounds": 0,
            },
            config,
        )
        commands = _intake_outcome_commands(
            ticket_id=ticket_id,
            actor_id="intake-agent",
            expected_version=ticket.version,
            result=result,
        )
        ticket = await runtime.tickets.transition_many(
            principal.tenant_id,
            commands,
            scopes={"ticket:system"},
        )
        snapshot = await runtime.intake_graph.aget_state(config)
        return _serialize_intake_result(ticket, result, snapshot)
    except Exception as exc:
        raise _map_domain_error(exc) from exc


@router.post("/{ticket_id}/resume")
async def resume_ticket_intake(
    ticket_id: str,
    payload: ResumeIntakeRequest,
    request: Request,
    principal: Principal = Depends(authenticate),
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
        raise HTTPException(status_code=409, detail="工单版本已变化，请刷新后重试")
    config = _intake_config(principal.tenant_id, ticket_id)
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
        server_command = payload.model_copy(update={"actor_id": principal.user_id})
        validated = validate_resume_command(pending, server_command, scopes=principal.scopes)
        result = await runtime.intake_graph.ainvoke(
            Command(resume=validated.resume_payload),
            config,
        )
        outcome = _intake_outcome_commands(
            ticket_id=ticket_id,
            actor_id="intake-agent",
            expected_version=ticket.version + 1,
            result=result,
        )
        ticket = await runtime.tickets.transition_many(
            principal.tenant_id,
            [validated.ticket_command, *outcome],
            scopes=principal.scopes | {"ticket:system"},
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
    principal: Principal = Depends(authenticate),
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
    principal: Principal = Depends(authenticate),
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
    principal: Principal = Depends(authenticate),
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
        return await runtime.tickets.transition(
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
    except Exception as exc:
        raise _map_domain_error(exc) from exc


@channel_router.post("/{channel}/events")
async def receive_channel_event(
    channel: str,
    payload: InboundChannelRequest,
    request: Request,
    principal: Principal = Depends(authenticate),
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
    try:
        event = adapter.verify_and_parse(
            await request.body(),
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
    try:
        event = adapter.verify_and_parse(
            await request.body(),
            timestamp=timestamp,
            signature=sign,
        )
        return await _persist_channel_event(_runtime(request), event, actor_id="dingtalk-webhook")
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise _map_domain_error(exc) from exc
