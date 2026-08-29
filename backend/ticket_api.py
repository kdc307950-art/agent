"""工单与渠道 HTTP API（FastAPI 路由）。

职责：
    - 工单 CRUD / 状态流转 / 建单受理（intake）/ 审批恢复（resume）
    - 满意度调查、资产绑定、IT 策略管理
    - 渠道入站事件（快速 ACK + 异步受理）、Outbox 死信管理
    - 企业微信 / 钉钉 webhook 验签与事件接收

关键设计：
    - 三个 router 分组：/tickets（工单）、/integrations（渠道）、/admin/it（策略管理）
    - 所有写操作走「受理工作流」（ticket_intake）或乐观锁状态机，服务端生成 operation_id
      与 expected_version，实现建单/受理幂等与并发安全
    - 权限按 scope 校验（ticket:customer / ticket:agent / ...），错误统一映射 _map_domain_error
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from src.my_agent.helpdesk import (
    ActorType,
    InvalidTicketTransition,
    TicketAction,
    TicketCommand,
    TicketPermissionDenied,
    TicketResumeCommand,
    TicketStatus,
)

from .channel_adapters import (
    DingTalkWebhookAdapter,
    IgnoreWebhookEvent,
    NormalizedChannelEvent,
    WebhookVerificationError,
    WeComWebhookAdapter,
)
from .security import Principal, enforce_rate_limit, rate_limit_dependency
from .ticket_intake import (
    apply_intake_resume,
    apply_operational_routing,
    classify_category,
    decode_cursor,
    deserialize_commands,
    encode_cursor,
    intake_config,
    intake_outcome_commands,
    pending_intake_interrupt,
    serialize_commands,
    serialize_intake_result,
)
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

# 三个路由分组：工单主路由 / 渠道集成 / 管理端 IT 策略
router = APIRouter(prefix="/tickets", tags=["tickets"])
channel_router = APIRouter(prefix="/integrations", tags=["integrations"])
admin_router = APIRouter(prefix="/admin/it", tags=["admin-it"])


class BindAssetRequest(BaseModel):
    """绑定资产请求：资产必须属于当前租户。"""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


class CreateTicketRequest(BaseModel):
    """Web 建单请求；ticket_id 缺省时由服务端生成。"""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    channel: str = Field(default="web", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    external_ticket_id: str | None = Field(default=None, min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=8_000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    asset_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class TransitionTicketRequest(BaseModel):
    """状态流转请求：action + expected_version（乐观锁）+ 操作者。"""

    model_config = ConfigDict(extra="forbid")

    action: TicketAction
    expected_version: int = Field(ge=0)
    actor_type: ActorType
    payload: dict[str, Any] = Field(default_factory=dict)


class StartIntakeRequest(BaseModel):
    """启动一次受理工作流：operation_id 保证幂等，text 为待受理内容。"""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=8_000)
    fields: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=0)


class ResumeIntakeRequest(TicketResumeCommand):
    """恢复被挂起的受理：operation_id 关联到具体工作流运行。"""

    operation_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class CreateSurveyRequest(BaseModel):
    """创建满意度调查：默认 7 天有效期。"""

    model_config = ConfigDict(extra="forbid")

    expires_in_days: int = Field(default=7, ge=1, le=90)


class ReplayOutboxRequest(BaseModel):
    """重放一条死信 Outbox 事件。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)


class SurveyResponseRequest(BaseModel):
    """满意度调查应答：1-5 分 + 可选反馈。"""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=5)
    feedback: str | None = Field(default=None, max_length=4_000)


class InboundChannelRequest(BaseModel):
    """渠道入站事件（通用 JSON 形态，供测试/扩展渠道使用）。"""

    model_config = ConfigDict(extra="forbid")

    external_event_id: str = Field(min_length=1, max_length=256)
    external_ticket_id: str | None = Field(default=None, min_length=1, max_length=256)
    requester_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=8_000)
    payload: dict[str, Any] = Field(default_factory=dict)


def _runtime(request: Request):
    """从 app.state 取运行时（含工单仓储），未就绪返回 503。"""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not hasattr(runtime, "tickets"):
        raise HTTPException(status_code=503, detail="工单服务尚未初始化")
    return runtime


def _require_scope(principal: Principal, scope: str) -> None:
    """校验调用方是否具备指定 scope，否则 403。"""
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"缺少 {scope} 权限")


def _actor_scope(actor_type: ActorType) -> str:
    """按操作者类型推导所需 scope：客户用 ticket:customer，坐席用 ticket:agent。"""
    return {
        ActorType.CUSTOMER: "ticket:customer",
        ActorType.AGENT: "ticket:agent",
        ActorType.APPROVER: "ticket:approve",
        ActorType.SYSTEM: "ticket:system",
    }[actor_type]


def _encode_cursor(updated_at: datetime, ticket_id: str) -> str:
    return encode_cursor(updated_at, ticket_id)


def _decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    try:
        return decode_cursor(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="无效分页游标") from exc


_intake_config = intake_config
_serialize_commands = serialize_commands
_deserialize_commands = deserialize_commands
_classify_category = classify_category
_intake_outcome_commands = intake_outcome_commands
_apply_operational_routing = apply_operational_routing
_serialize_intake_result = serialize_intake_result
_pending_intake_interrupt = pending_intake_interrupt


def _interrupt_payload(snapshot: object, interrupt_id: str) -> tuple[object, dict[str, Any]]:
    """在状态快照中按 interrupt_id 查找挂起的审批项及其 value。

    找不到时返回 409：客户端持有的恢复标识已过期或被其他审批消费。
    """
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            if str(getattr(item, "id", "") or "") == interrupt_id:
                value = getattr(item, "value", None)
                if isinstance(value, dict):
                    return item, value
    raise HTTPException(status_code=409, detail="恢复标识已失效，请刷新后重试")


async def _webhook_body(request: Request, channel: str) -> bytes:
    """读取并限制 webhook 请求体（≤256 KB），并按来源 IP 做渠道级限流。"""
    source_ip = request.client.host if request.client is not None else "unknown"
    await enforce_rate_limit(request, f"webhook:{channel}:{source_ip}", f"webhook:{channel}")
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > 256 * 1024:
        raise HTTPException(status_code=413, detail="Webhook 请求体超过 256 KB")
    body = await request.body()
    if len(body) > 256 * 1024:
        raise HTTPException(status_code=413, detail="Webhook 请求体超过 256 KB")
    return body


def _event_payload(event: NormalizedChannelEvent) -> dict[str, Any]:
    """把渠道事件序列化为 inbound_events.payload，供 InboundWorker 重建事件。"""
    return {
        "requester_id": event.requester_id,
        "external_ticket_id": event.external_ticket_id,
        "title": event.title,
        "content": event.content,
        "channel": event.channel,
        "raw": event.payload,
    }


async def _ack_channel_event(
    runtime, event: NormalizedChannelEvent, *, actor_id: str
) -> dict[str, Any]:
    """渠道入站快速 ACK：只登记事件（received）并返回，业务由 InboundWorker 异步执行。

    幂等：同 (tenant, channel, external_event_id) 重复登记返回原记录（created=False）。
    """
    result = await runtime.tickets.register_inbound_event(
        event.tenant_id,
        event.channel,
        event.external_event_id,
        _event_payload(event),
    )
    return {"accepted": True, "event_id": event.external_event_id, "created": result.created}


def _map_domain_error(exc: Exception) -> HTTPException:
    """把仓储/受理层领域异常统一映射为 HTTP 状态码。

    404：工单/资产不存在（AssetBindingError 也归 404，不暴露资产归属信息）；
    403：无权限；409：版本冲突/重复建单/非法流转；其余 500。
    """
    if isinstance(exc, (TicketNotFound, AssetBindingError)):
        # AssetBindingError 与 TicketNotFound 同返回 404：不暴露资产是否存在或归属谁。
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, TicketPermissionDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(
        exc,
        (
            TicketAlreadyExists,
            TicketVersionConflict,
            TicketCapacityExceeded,
            InboundEventConflict,
            InvalidTicketTransition,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="工单服务内部错误")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: CreateTicketRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """Web 渠道建单：客户身份，ticket_id 缺省时服务端生成。

    注意这是「直接落库」的简单建单路径；带分类/路由/SLA 的完整受理走
    POST /tickets/{ticket_id}/intake（受理工作流）。
    """
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
    """工单列表：坐席看全量并按团队/成员筛选，客户只看本人工单。

    游标分页：取 limit+1 行判断 has_more，返回 (updated_at, ticket_id) 编码的
    next_cursor；过滤条件（team/user/priority）仅对坐席生效，客户恒被限制为
    requester_id = 本人。
    """
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
        assigned_user_id=(
            (principal.user_id if assigned_user_id == "current_user" else assigned_user_id)
            if is_agent
            else None
        ),
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
    """工单详情：坐席可读任意工单，客户仅能读本人工单（否则 404 避免探测）。"""
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
    """工单概览聚合：SLA + 满意度 + 消息 + 指派记录 + 状态流水 + RAG 引用。

    详情页一次请求拿到全部上下文，减少往返；权限同 get_ticket。
    """
    if not ({"ticket:customer", "ticket:agent"} & principal.scopes):
        raise HTTPException(status_code=403, detail="缺少工单读取权限")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or (
        "ticket:agent" not in principal.scopes and ticket.requester_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="工单不存在")
    overview = await runtime.ticket_operations.get_ticket_overview(principal.tenant_id, ticket_id)
    overview["status_events"] = await runtime.tickets.list_status_events(
        principal.tenant_id, ticket_id
    )
    return overview


@router.get("/{ticket_id}/pending-interrupt")
async def get_pending_ticket_interrupt(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """查询工单当前是否有挂起的受理审批（interrupt），供前端恢复审批卡片。"""
    if not ({"ticket:customer", "ticket:agent"} & principal.scopes):
        raise HTTPException(status_code=403, detail="缺少工单读取权限")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or (
        "ticket:agent" not in principal.scopes and ticket.requester_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="工单不存在")
    return {"interrupt": await _pending_intake_interrupt(runtime, principal.tenant_id, ticket_id)}


@router.get("/{ticket_id}/intake-status")
async def get_ticket_intake_status(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """查询工单受理状态与待补全关联（含过期时间、resume 次数）。"""
    if not ({"ticket:customer", "ticket:agent"} & principal.scopes):
        raise HTTPException(status_code=403, detail="缺少工单读取权限")
    runtime = _runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None or (
        "ticket:agent" not in principal.scopes and ticket.requester_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="工单不存在")
    pending = await runtime.tickets.get_pending_intake(principal.tenant_id, ticket_id)
    return {
        "ticket_id": ticket_id,
        "status": ticket.status,
        "pending_intake": pending,
        "interrupt": await _pending_intake_interrupt(runtime, principal.tenant_id, ticket_id),
    }


@router.post("/{ticket_id}/intake")
async def start_ticket_intake(
    ticket_id: str,
    payload: StartIntakeRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """启动（或幂等重放）一次受理工作流。

    核心流程：
        1. operation_id + expected_version 双重校验；已 committed 的 operation 幂等返回
        2. 运行 intake_graph（分类/澄清/路由），产出待执行的工单命令序列
        3. record_workflow_intent 落库意图（供恢复/审计），再 transition_many 原子执行
        4. 进入 queued/assigned 时按分类创建 SLA 实例

    若受理中途出现 human_approval 挂起，返回的 interrupt 由客户端展示审批卡片，
    之后通过 POST /{ticket_id}/resume 恢复。
    """
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
            # 幂等重试：操作已提交，直接返回当前状态，不再重复建单/受理
            return {
                "ticket": ticket,
                "state": {},
                "interrupt": await _pending_intake_interrupt(
                    runtime, principal.tenant_id, ticket_id
                ),
            }
        raise HTTPException(status_code=409, detail="工单版本已变化，请刷新后重试")
    try:
        run = await runtime.tickets.start_workflow_operation(
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            operation_id=payload.operation_id,
            command_type="intake",
            expected_version=ticket.version,
            checkpoint_thread_id=_intake_config(principal.tenant_id, ticket_id)["configurable"][
                "thread_id"
            ],
        )
        if run["status"] == "committed":
            # 并发下另一个请求已完成同一 operation：幂等返回
            return {
                "ticket": ticket,
                "state": {},
                "interrupt": await _pending_intake_interrupt(
                    runtime, principal.tenant_id, ticket_id
                ),
            }
        if run["intent"] is not None:
            # 恢复路径：意图已记录，反序列化命令直接执行，不重新跑图
            commands = _deserialize_commands(run["intent"])
            result = dict(run["intent"].get("result") or {})
        else:
            if ticket.status not in {TicketStatus.NEW, TicketStatus.INTAKING}:
                raise InvalidTicketTransition(f"状态 {ticket.status} 不能启动受理")
            config = _intake_config(principal.tenant_id, ticket_id)
            # 首次受理：运行 LangGraph 受理图（分类 + 澄清 + 决策）
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
            prefix = (
                []
                if ticket.status == TicketStatus.INTAKING
                else [
                    # 从 new -> intaking 需要一个前置动作（若尚未进入受理状态）
                    TicketCommand(
                        ticket_id=ticket_id,
                        action=TicketAction.START_INTAKE,
                        actor_type=ActorType.SYSTEM,
                        actor_id="intake-agent",
                        expected_version=ticket.version,
                    )
                ]
            )
            next_version = ticket.version + len(prefix)
            commands = [
                *prefix,
                *_intake_outcome_commands(
                    ticket_id=ticket_id,
                    actor_id="intake-agent",
                    expected_version=next_version,
                    result=result,
                ),
            ]
            # 受理产出命令后再做运营路由（分类匹配路由规则 -> 目标团队/坐席）
            commands = await _apply_operational_routing(
                runtime,
                commands,
                result,
                tenant_id=principal.tenant_id,
                channel=getattr(ticket, "channel", "web"),
            )
            await runtime.tickets.record_workflow_intent(
                tenant_id=principal.tenant_id,
                ticket_id=ticket_id,
                operation_id=payload.operation_id,
                intent={
                    "commands": _serialize_commands(commands),
                    "result": {
                        key: value for key, value in result.items() if key != "__interrupt__"
                    },
                },
            )
        # 原子执行命令序列：乐观锁 + operation_id 防重复提交
        ticket = await runtime.tickets.transition_many(
            principal.tenant_id,
            commands,
            scopes={"ticket:system"},
            operation_id=payload.operation_id,
        )
        if ticket.status in {TicketStatus.QUEUED, TicketStatus.ASSIGNED}:
            # 受理落定后创建 SLA 实例（业务日历计算截止时间）
            await runtime.ticket_operations.ensure_sla_for_ticket(
                tenant_id=principal.tenant_id,
                ticket_id=ticket.ticket_id,
                channel=getattr(ticket, "channel", "web"),
                category=getattr(ticket, "category", None),
            )
        snapshot = await runtime.intake_graph.aget_state(
            _intake_config(principal.tenant_id, ticket_id)
        )
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
    """恢复被挂起的受理：客户补全信息后继续走完受理工作流。

    校验：恢复命令必须属于当前工单、actor 必须是 customer、expected_version 匹配；
    已 committed 的 operation 幂等返回。成功后返回最新工单 + 状态 + 残留 interrupt。
    """
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
            return {
                "ticket": ticket,
                "state": {},
                "interrupt": await _pending_intake_interrupt(
                    runtime, principal.tenant_id, ticket_id
                ),
            }
        raise HTTPException(status_code=409, detail="工单版本已变化，请刷新后重试")
    try:
        server_command = payload.model_copy(update={"actor_id": principal.user_id})
        outcome = await apply_intake_resume(
            runtime,
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            interrupt_id=payload.interrupt_id,
            resume_command=server_command,
            operation_id=payload.operation_id,
            expected_version=ticket.version,
            scopes=set(principal.scopes),
            channel=getattr(ticket, "channel", "web"),
        )
        return {
            "ticket": outcome["ticket"],
            "state": outcome["result"],
            "interrupt": outcome["interrupt"],
        }
    except (
        ValueError,
        TicketPermissionDenied,
        TicketVersionConflict,
        InvalidTicketTransition,
    ) as exc:
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
    """坐席发起满意度调查；每工单最多一份（409），写调查 + Outbox 投递事件。"""
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    if await runtime.tickets.get(principal.tenant_id, ticket_id) is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    survey_id = uuid4().hex
    created = await runtime.ticket_operations.create_survey(
        tenant_id=principal.tenant_id,
        ticket_id=ticket_id,
        survey_id=survey_id,
        expires_at=datetime.now(UTC) + timedelta(days=payload.expires_in_days),
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
    """客户提交满意度评分（1-5 分）；仅工单请求人可答，过期/重复返回 409。"""
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
    """通用状态流转端点（接单/处理/解决/关闭/指派等）。

    按 actor_type 推导所需 scope（客户/坐席/审批人/系统），乐观锁 expected_version
    防并发覆盖；客户仅能操作本人工单。状态机合法性由 transition_ticket 校验。
    """
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
            await runtime.ticket_operations.pause_sla(
                principal.tenant_id, ticket_id, reason="awaiting_customer"
            )
        elif updated.status in {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED}:
            await runtime.ticket_operations.resume_sla(
                principal.tenant_id, ticket_id, resumed_at=datetime.now(UTC)
            )
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
    """列出租户的死信 Outbox 事件（投递多次失败后进入 dead 状态）。"""
    _require_scope(principal, "ticket:agent")
    return {
        "items": await _runtime(request).ticket_operations.list_dead_outbox(
            tenant_id=principal.tenant_id, limit=limit
        )
    }


@channel_router.post("/outbox/replay")
async def replay_dead_outbox_event(
    payload: ReplayOutboxRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """手动重放一条死信事件（回到 pending，等待 Worker 重新投递）。"""
    _require_scope(principal, "ticket:agent")
    replayed = await _runtime(request).ticket_operations.replay_dead_outbox(
        principal.tenant_id, payload.event_id
    )
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
    """通用渠道入站端点（快速 ACK）：登记事件即回 202，建单/受理异步执行。

    供测试与自定义渠道使用；企业微信/钉钉走各自的专用 webhook 端点。
    """
    _require_scope(principal, "ticket:channel")
    runtime = _runtime(request)
    try:
        result = await _ack_channel_event(
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
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result)


@channel_router.get("/events/{event_id}")
async def get_inbound_event_status(
    event_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """查询渠道入站事件处理状态（异步 ACK 后由调用方轮询获取 ticket_id）。"""
    _require_scope(principal, "ticket:channel")
    runtime = _runtime(request)
    items = await runtime.tickets.list_inbound_events(principal.tenant_id, event_id)
    return {
        "event_id": event_id,
        "items": [
            {
                "channel": item["channel"],
                "status": item["status"],
                "ticket_id": item["ticket_id"],
                "attempts": item["attempts"],
                "error_code": item["error_code"],
                "created_at": item["received_at"],
                "processed_at": item["processed_at"],
            }
            for item in items
        ],
    }


@channel_router.get("/wecom/webhook", response_class=PlainTextResponse)
async def verify_wecom_webhook(
    request: Request,
    timestamp: str = Query(min_length=1, max_length=20),
    nonce: str = Query(min_length=1, max_length=128),
    msg_signature: str = Query(min_length=1, max_length=128),
    echostr: str = Query(min_length=1, max_length=4096),
):
    """企业微信后台「保存回调 URL」的 GET 验证：验签 + AES 解密 echostr 并原样回显。

    只做验证与解密，不建单、不访问业务表；失败统一 401（不暴露配置差异）。
    """
    settings = request.app.state.settings
    if not all(
        (
            settings.wecom_tenant_id,
            settings.wecom_token,
            settings.wecom_encoding_aes_key,
            settings.wecom_corp_id,
        )
    ):
        raise HTTPException(status_code=503, detail="企业微信 Webhook 未配置")
    adapter = WeComWebhookAdapter(
        tenant_id=settings.wecom_tenant_id,
        token=settings.wecom_token,
        encoding_aes_key=settings.wecom_encoding_aes_key,
        corp_id=settings.wecom_corp_id,
        replay_window_seconds=settings.webhook_replay_window_seconds,
    )
    try:
        return PlainTextResponse(
            adapter.verify_url(
                timestamp=timestamp,
                nonce=nonce,
                signature=msg_signature,
                echostr=echostr,
            )
        )
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@channel_router.post("/wecom/webhook")
async def receive_wecom_webhook(
    request: Request,
    timestamp: str = Query(min_length=1, max_length=20),
    nonce: str = Query(min_length=1, max_length=128),
    msg_signature: str = Query(min_length=1, max_length=128),
):
    """企业微信事件推送：验签 + 解密 -> 归一化 -> 快速 ACK（202）。

    非文本事件（进入应用/位置上报等）验签通过后返回 200 但不登记不建单，
    阻止企微无限重试；验签失败返回 401。
    """
    settings = request.app.state.settings
    if not all(
        (
            settings.wecom_tenant_id,
            settings.wecom_token,
            settings.wecom_encoding_aes_key,
            settings.wecom_corp_id,
        )
    ):
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
        result = await _ack_channel_event(_runtime(request), event, actor_id="wecom-webhook")
    except IgnoreWebhookEvent as exc:
        # 事件消息（进入应用/位置上报等）：验签已通过，返回 200 阻止企微重试，不登记、不建单。
        return {"ignored": True, "event": exc.event}
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise _map_domain_error(exc) from exc
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result)


@channel_router.post("/dingtalk/webhook")
async def receive_dingtalk_webhook(
    request: Request,
    timestamp: str = Query(min_length=1, max_length=20),
    sign: str = Query(min_length=1, max_length=512),
):
    """钉钉机器人回调：HMAC 验签 -> 归一化 -> 快速 ACK（202）。"""
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
        result = await _ack_channel_event(_runtime(request), event, actor_id="dingtalk-webhook")
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise _map_domain_error(exc) from exc
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result)


# ========== IT 策略管理（/admin/it/policies） ==========


@admin_router.get("/pending-intakes")
async def list_pending_intakes(
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """管理端查看客户待补全关联（工单、过期时间、resume 次数）。"""
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    items = await runtime.tickets.list_pending_intakes(principal.tenant_id)
    return {"items": items}


@admin_router.get("/policies")
async def list_it_policies(
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """列出租户所有生效的 IT 策略（SLA/路由匹配基础）。"""
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
    """删除指定分类的 IT 策略；写审计事件。"""
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
    """查询单个分类的 IT 策略详情。"""
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
    """创建/更新 IT 策略（按分类 upsert）；写审计事件。"""
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
    """坐席将资产绑定到工单；资产必须属于当前租户，否则 404。"""
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
    """解绑工单资产（坐席操作）。"""
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    try:
        return await runtime.tickets.unbind_asset(principal.tenant_id, ticket_id)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
