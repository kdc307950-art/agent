"""LANGGraph VPN 客户处置闭环 HTTP API（阶段二，api_v2）。

低侵入、可运行的端到端入口，接 runtime.vpn_closed_loop（VpnClosedLoopService）+ audit：

    - POST /tickets/{ticket_id}/vpn/diagnose          触发一次 VPN 诊断并落库 + 状态机联动
    - GET  /tickets/{ticket_id}/vpn/diagnosis         查询工单的处置闭环快照
    - POST /tickets/{ticket_id}/vpn/actions/{action_id}/result  客户回填一条排查步骤结果
    - POST /tickets/{ticket_id}/vpn/diagnose/resume   （客户动作后）再次诊断

设计要点（参照 backend/vpn/api.py 的 _runtime/_run_context/_require_scope 模式）：
    - 路由归属 ticket 域：router prefix="/tickets"，与既有 /vpn/reissue 前缀互不冲突。
    - 租户隔离：principal.tenant_id 为权威，service 层只按 principal.tenant_id 读/写工单，
      请求体不信任租户（本路由请求体只含客户动作，不含租户字段）。
    - scope 校验：诊断动作（agent）需 ticket:agent；客户回填结果需 ticket:customer 且为本人工单。
    - 错误映射：404 工单不存在 / 409 状态冲突（重复回填/非法动作）/ 422 参数非法（ValueError）。
    - 每个关键点写 audit.record_event（vpn_diagnosis_*，由 closed_loop 服务负责）。
    - 服务未初始化（未配置模型 key）返回 503。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.run_context import RunContext
from backend.security import Principal, rate_limit_dependency

from .closed_loop import VpnCustomerActionError, VpnDiagnosisNotFound

router = APIRouter(prefix="/tickets", tags=["vpn-diagnosis"])


class VpnActionResultRequest(BaseModel):
    """客户回填一条排查步骤的执行结果。"""

    model_config = ConfigDict(extra="forbid")

    result: str = Field(min_length=1, max_length=16_000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    details: str = Field(default="", max_length=4_000)


class VpnResumeRequest(BaseModel):
    """再次诊断请求体（可携带动作快照等，暂为保留结构）。"""

    model_config = ConfigDict(extra="forbid")

    comment: str = Field(default="", max_length=2_000)


def _runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    svc = getattr(runtime, "vpn_closed_loop", None) if runtime else None
    if svc is None:
        raise HTTPException(status_code=503, detail="VPN 处置闭环服务尚未初始化")
    return runtime


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"缺少 {scope} 权限")


def _run_context(principal: Principal, *, ticket_id: str) -> RunContext:
    return RunContext(
        run_id=f"vpn-closed-loop-{uuid.uuid4().hex[:12]}",
        request_id=uuid.uuid4().hex[:12],
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        thread_id=f"vpn:{principal.tenant_id}:{ticket_id}",
        scopes=frozenset(principal.scopes),
        deadline=time.time() + 60,
        allowed_tools=None,
    )


async def _ticket_or_404(runtime, tenant_id: str, ticket_id: str, *, requester_id: str | None = None):
    tickets = getattr(runtime, "tickets", None)
    if tickets is None or not hasattr(tickets, "get"):
        raise HTTPException(status_code=503, detail="工单服务尚未初始化")
    ticket = await tickets.get(tenant_id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if requester_id is not None and getattr(ticket, "requester_id", None) != requester_id:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ticket


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VpnDiagnosisNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, VpnCustomerActionError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="VPN 处置闭环服务内部错误")


@router.post("/{ticket_id}/vpn/diagnose", status_code=201)
async def diagnose_ticket(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """触发一次 VPN 诊断：落库 VpnDiagnosisRun + 联动工单状态机。"""
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    await _ticket_or_404(runtime, principal.tenant_id, ticket_id)
    ctx = _run_context(principal, ticket_id=ticket_id)
    try:
        return await runtime.vpn_closed_loop.diagnose(
            runtime=runtime,
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            run_context=ctx,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/{ticket_id}/vpn/diagnose/resume")
async def resume_diagnosis(
    ticket_id: str,
    payload: VpnResumeRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """客户动作后再次诊断（新 run）。"""
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    await _ticket_or_404(runtime, principal.tenant_id, ticket_id)
    ctx = _run_context(principal, ticket_id=ticket_id)
    try:
        return await runtime.vpn_closed_loop.resume(
            runtime=runtime,
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            run_context=ctx,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/{ticket_id}/vpn/diagnosis")
async def get_diagnosis(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """查询工单的 VPN 处置闭环快照（诊断运行/排查步骤/结果/升级）。

    只读查询：坐席（ticket:agent）或客户（ticket:customer，仅本人工单）均可读。
    """
    if not ({"ticket:agent", "ticket:customer"} & principal.scopes):
        raise HTTPException(status_code=403, detail="缺少工单读取权限")
    runtime = _runtime(request)
    requester_id = None if "ticket:agent" in principal.scopes else principal.user_id
    await _ticket_or_404(runtime, principal.tenant_id, ticket_id, requester_id=requester_id)
    return await runtime.vpn_closed_loop.get_snapshot(
        tenant_id=principal.tenant_id,
        ticket_id=ticket_id,
    )


@router.post("/{ticket_id}/vpn/actions/{action_id}/result")
async def submit_action_result(
    ticket_id: str,
    action_id: str,
    payload: VpnActionResultRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """客户回填一条排查步骤的执行结果（awaiting_customer_action -> diagnosing + 再次诊断）。"""
    _require_scope(principal, "ticket:customer")
    runtime = _runtime(request)
    await _ticket_or_404(runtime, principal.tenant_id, ticket_id, requester_id=principal.user_id)
    ctx = _run_context(principal, ticket_id=ticket_id)
    try:
        return await runtime.vpn_closed_loop.submit_action_result(
            runtime=runtime,
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            action_id=action_id,
            result=payload.result,
            evidence=payload.evidence,
            details=payload.details,
            run_context=ctx,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
