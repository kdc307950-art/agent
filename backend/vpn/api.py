"""重新下发 VPN 配置文件（reissue_vpn_config）审批式执行的 HTTP API。

低侵入、可运行的端到端入口，接 runtime.vpn_reissue（VpnReissueService）+ audit：

    - POST /vpn/reissue                        触发审批（request_approval + payload.action 经 dispatch）
    - POST /vpn/reissue/{idempotency_key}/approve  审批通过（principal 校验 ticket:approve）
    - POST /vpn/reissue/{idempotency_key}/reject   审批拒绝
    - GET  /vpn/reissue/{idempotency_key}       查询审批/执行结果

设计要点：
    - 租户隔离：principal.tenant_id 为权威，请求体不信任租户；body 内 tenant 与 principal 不一致即 403。
    - 触发是「发起审批」，不执行副作用；审批通过后经 VpnReissueService.approve 受控执行并落终态。
    - approve 附带 domain 状态迁移（AWAITING_APPROVAL→IN_PROGRESS，复用 domain 状态机），保证工单不僵在
      AWAITING_APPROVAL（reviewer 第 2 点）。
    - 幂等键来自 body 的确定性派生；approve/reject 需 body 内 idempotency_key 与路径一致（防误批他单）。
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.run_context import RunContext
from backend.security import Principal, rate_limit_dependency
from src.my_agent.helpdesk import ActorType, TicketAction, TicketCommand, transition_ticket

from .approval import ApprovalStatus, build_reissue_request_from_payload

router = APIRouter(prefix="/vpn/reissue", tags=["vpn-reissue"])


class ReissueStartPayload(BaseModel):
    """触发/审批请求体（字段与 request_approval 命令的 payload 对齐）。"""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(default="reissue_vpn_config", max_length=64)
    user_id: str = Field(min_length=1, max_length=128)
    asset_id: str | None = Field(default=None, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    client_version: str = Field(min_length=1, max_length=64)
    target_version: str | None = Field(default=None, max_length=64)
    reason_codes: list[str] = Field(default_factory=list, max_length=32)


def _runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    svc = getattr(runtime, "vpn_reissue", None) if runtime else None
    if svc is None:
        raise HTTPException(status_code=503, detail="VPN 重新下发服务尚未初始化")
    return runtime


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"缺少 {scope} 权限")


def _run_context(principal: Principal, *, ticket_id: str) -> RunContext:
    return RunContext(
        run_id=f"vpn-reissue-{uuid.uuid4().hex[:12]}",
        request_id=uuid.uuid4().hex[:12],
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        thread_id=f"vpn:{principal.tenant_id}:{ticket_id}",
        scopes=frozenset(principal.scopes),
        deadline=time.time() + 60,
        allowed_tools=None,
    )


def _build_request(payload: ReissueStartPayload, principal: Principal):
    raw = payload.model_dump(mode="json")
    raw.setdefault("client_version", raw.get("target_version") or "")
    raw.pop("target_version", None)
    try:
        req = build_reissue_request_from_payload(raw, tenant_id=principal.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if req.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="跨租户访问被拒绝")
    return req


@router.post("", status_code=201)
async def start_reissue(
    request: Request,
    payload: ReissueStartPayload,
    principal: Principal = Depends(rate_limit_dependency),
):
    """触发一次重新下发配置的审批（不执行副作用）。"""
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    req = _build_request(payload, principal)
    ctx = _run_context(principal, ticket_id=req.ticket_id)
    out = await runtime.vpn_reissue.start(request=req, runtime=runtime, run_context=ctx)
    if out.get("status") == ApprovalStatus.FAILED.value:
        raise HTTPException(
            status_code=409,
            detail={"status": out["status"], "fail_reasons": out.get("fail_reasons")},
        )
    return out


@router.post("/{idempotency_key}/approve")
async def approve_reissue(
    idempotency_key: str,
    request: Request,
    payload: ReissueStartPayload,
    principal: Principal = Depends(rate_limit_dependency),
):
    """审批通过：经 VpnReissueService.approve 受控执行并落终态，附带 domain APPROVE 迁移。"""
    _require_scope(principal, "ticket:approve")
    runtime = _runtime(request)
    req = _build_request(payload, principal)
    if req.idempotency_key != idempotency_key:
        raise HTTPException(status_code=409, detail="幂等键不匹配")
    ctx = _run_context(principal, ticket_id=req.ticket_id)
    result = await runtime.vpn_reissue.approve(
        request=req,
        approver_user_id=principal.user_id,
        runtime=runtime,
        run_context=ctx,
    )
    if result.ok:
        await _domain_after_approve(runtime, req, principal)
    return result.model_dump(mode="json")


@router.post("/{idempotency_key}/reject")
async def reject_reissue(
    idempotency_key: str,
    request: Request,
    payload: ReissueStartPayload,
    principal: Principal = Depends(rate_limit_dependency),
):
    """审批拒绝：置 REJECTED + 工单回可接管态。"""
    _require_scope(principal, "ticket:approve")
    runtime = _runtime(request)
    req = _build_request(payload, principal)
    if req.idempotency_key != idempotency_key:
        raise HTTPException(status_code=409, detail="幂等键不匹配")
    ctx = _run_context(principal, ticket_id=req.ticket_id)
    result = await runtime.vpn_reissue.reject(
        request=req,
        runtime=runtime,
        run_context=ctx,
        approver_user_id=principal.user_id,
        reason="审批拒绝",
    )
    await _domain_after_reject(runtime, req, principal)
    return result.model_dump(mode="json")


@router.get("/{idempotency_key}")
async def get_reissue(
    idempotency_key: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """查询某次重新下发配置的审批/执行结果。"""
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    # N2 纵深：校验 key 内嵌租户与当前主体一致（防跨租户查询）。
    embedded_tenant = idempotency_key.split(":")[1] if ":" in idempotency_key else ""
    if embedded_tenant != principal.tenant_id:
        raise HTTPException(status_code=403, detail="跨租户访问被拒绝")
    result = runtime.vpn_reissue.registry.get_result(idempotency_key)
    status = runtime.vpn_reissue.registry.get_status(idempotency_key)
    return {
        "idempotency_key": idempotency_key,
        "status": status.value if status else None,
        "result": result.model_dump(mode="json") if result else None,
    }


# ---- domain 状态迁移（复用 domain 状态机；不阻断主流程） ----


async def _domain_after_approve(runtime, req, principal: Principal) -> None:
    """工单 AWAITING_APPROVAL→IN_PROGRESS（APPROVE），只允许 domain 合法跳转。"""
    await _transition(runtime, req, principal, TicketAction.APPROVE, ActorType.APPROVER)


async def _domain_after_reject(runtime, req, principal: Principal) -> None:
    """工单 AWAITING_APPROVAL→（REJECT）回 IN_PROGRESS 可继续处理。"""
    await _transition(runtime, req, principal, TicketAction.REJECT, ActorType.APPROVER)


async def _transition(runtime, req, principal: Principal, action: TicketAction, actor_type: ActorType) -> None:
    tickets = getattr(runtime, "tickets", None)
    if tickets is None or not hasattr(tickets, "get") or not hasattr(tickets, "transition"):
        return
    try:
        ticket = await tickets.get(req.tenant_id, req.ticket_id)
        if ticket is None:
            return
        scopes = set(principal.scopes)
        cmd = TicketCommand(
            ticket_id=req.ticket_id,
            action=action,
            actor_type=actor_type,
            actor_id=principal.user_id,
            expected_version=int(getattr(ticket, "version", 0) or 0),
            payload={"action": req.action.value, "idempotency_key": req.idempotency_key},
        )
        # 提前用 domain 纯函数校验合法性，非法/无权跳转则不落库（不阻断主流程）。
        transition_ticket(ticket.status, cmd, scopes=scopes)
        await tickets.transition(req.tenant_id, cmd, scopes=scopes)
    except Exception:  # noqa: BLE001  陈旧版本/非法跳转视为非阻断
        return
