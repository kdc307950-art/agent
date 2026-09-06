"""重新下发 VPN 配置文件（reissue_vpn_config）审批式执行的 HTTP API。

低侵入、可运行的端到端入口，接 runtime.vpn_reissue（VpnReissueService）+ audit：

    - POST /vpn/reissue                         触发审批（request_approval + payload.action 经 dispatch）
    - POST /vpn/reissue/{operation_id}/approve  审批通过（body = {"operation_id": ..., "decision": "approve"}）
    - POST /vpn/reissue/{operation_id}/reject   审批拒绝（body = {"operation_id": ..., "decision": "reject"}）
    - GET  /vpn/reissue/{operation_id}          从 store 查询审批/执行结果（operation_id）

设计要点（对齐「只诊断、不自动执行」+ 审批式执行六项硬验收）：
    - 请求体最小化：approve/reject 只接受 {operation_id, decision}（``ApprovalDecision``，
      extra="forbid"），不再信任/携带完整请求快照；请求内容一律从 store/DB 的
      request_snapshot 重建（防篡改）。
    - 存储为唯一权威：GET/approve/reject/reconcile 的读都经 runtime.vpn_reissue.store
      （ReissueStore），不再直接读内存登记表 registry（registry 仅作向后兼容回退）。
    - 租户隔离：principal.tenant_id 为权威；operation_id 内嵌租户（"reissue:{tenant}:..."）
      需与 principal 一致（否则 403），并以 store 的 (tenant_id, operation_id) 读取兜底，
      跨租户一律 403，同租户但不存在则 404/409。
    - 触发是「发起审批」，不执行副作用；审批通过后经 VpnReissueService.approve 受控执行并落终态。
    - approve 附带 domain 状态迁移（AWAITING_APPROVAL→IN_PROGRESS），保证工单不僵在
      AWAITING_APPROVAL。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.run_context import RunContext
from backend.security import Principal, rate_limit_dependency
from src.my_agent.helpdesk import ActorType, TicketAction, TicketCommand, transition_ticket

from .approval import (
    ApprovalStatus,
    ReissueExecutionResult,
    _operation_result,
    _operation_to_request,
    build_reissue_request_from_payload,
)

router = APIRouter(prefix="/vpn/reissue", tags=["vpn-reissue"])


class ReissueStartPayload(BaseModel):
    """触发/审批请求体（字段与 request_approval 命令的 payload 对齐；仅 start 用）。"""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(default="reissue_vpn_config", max_length=64)
    user_id: str | None = Field(default=None, max_length=128)
    asset_id: str | None = Field(default=None, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    client_version: str = Field(default="tenant-current", min_length=1, max_length=64)
    target_version: str | None = Field(default=None, max_length=64)
    reason_codes: list[str] = Field(default_factory=list, max_length=32)


class ApprovalDecision(BaseModel):
    """审批动作的最小请求体（approve/reject 共用）。

    extra="forbid"：拒绝模型/调用方注入完整请求快照或身份字段；请求内容一律从
    store/DB 的 request_snapshot 重建。``operation_id`` 必须与路径一致（防误批他单）。
    """

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=256)
    decision: Literal["approve", "reject"]


class SubmissionConfirmation(BaseModel):
    """人工在 FMG Task Manager 核对后的提交事实。"""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=256)
    submission_state: Literal["submitted", "not_submitted"]
    vendor_task_id: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=1000)


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


def _embedded_tenant(operation_id: str) -> str:
    """从 operation_id 提取内嵌租户（格式 'reissue:{tenant}:{user}:{asset}:{ticket}:{version}'）。"""
    return operation_id.split(":")[1] if ":" in operation_id else ""


def _assert_tenant(operation_id: str, principal: Principal) -> None:
    """内嵌租户与当前主体不一致 -> 403（operation_id 结构的纵深校验）。"""
    embedded_tenant = _embedded_tenant(operation_id)
    if embedded_tenant and embedded_tenant != principal.tenant_id:
        raise HTTPException(status_code=403, detail="跨租户访问被拒绝")


def _raise_transition_error(result: ReissueExecutionResult) -> None:
    """把 store 执行结果映射为 HTTP 异常；返回 None 表示放行（200）。

    SUCCESS / ALREADY_TARGET_STATE（幂等成功）→ 200；
    REJECTED / CANCELLED（终态人工接管动作成功）→ 200；
    ILLEGAL_TRANSITION / VERSION_CONFLICT / not_found / missing_request_snapshot /
    not_approved / preflight_failed → 409；
    DB_ERROR → 503；denied_scope → 403；unsupported_decision → 400；其余兜底 500。
    """
    code = result.error_code
    if result.ok:
        return
    if code in ("already_target_state", "rejected", "cancelled"):
        # 幂等成功 / 终态动作成功：以 200 放行（允许调用方读回既有/终态结果）。
        return
    if code in (
        "illegal_transition",
        "version_conflict",
        "not_found",
        "missing_request_snapshot",
        "not_approved",
        "preflight_failed",
        "precheck_failed",
    ):
        transition_detail: dict[str, Any] = {"error_code": code, "reason": result.reason}
        if getattr(result, "detail", None):
            fail_reasons = result.detail.get("fail_reasons")
            if fail_reasons:
                transition_detail["fail_reasons"] = fail_reasons
        raise HTTPException(status_code=409, detail=transition_detail)
    if code == "db_error":
        raise HTTPException(status_code=503, detail={"error_code": code, "reason": result.reason})
    if code in ("denied_scope", "high_risk_requires_human"):
        scope_detail: dict[str, Any] = {"error_code": code, "reason": result.reason}
        if getattr(result, "detail", None):
            high_risk = result.detail.get("high_risk_reasons")
            if high_risk:
                scope_detail["high_risk_reasons"] = high_risk
        raise HTTPException(status_code=403, detail=scope_detail)
    if code == "unsupported_decision":
        raise HTTPException(status_code=400, detail={"error_code": code, "reason": result.reason})
    # 兜底：未识别的结构错误
    raise HTTPException(status_code=500, detail={"error_code": code or "unknown", "reason": result.reason})


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


async def start_tenant_redeploy_from_ticket(
    *,
    request: Request,
    principal: Principal,
    ticket_id: str,
    expected_version: int,
    reason_codes: list[str],
) -> dict[str, Any]:
    """Create a tenant redeploy approval from an already-authorized VPN ticket.

    The caller owns ticket visibility/category/state checks. This helper owns the
    control-plane contract: no per-user fields, deterministic tenant+ticket
    idempotency, preflight preview, approval registration, and audit context.
    """
    runtime = _runtime(request)
    raw = {
        "action": "redeploy_tenant_vpn_config",
        "ticket_id": ticket_id,
        "client_version": "tenant-current",
        "reason_codes": reason_codes,
    }
    try:
        redeploy_request = build_reissue_request_from_payload(
            raw,
            tenant_id=principal.tenant_id,
            expected_version=expected_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    context = _run_context(principal, ticket_id=redeploy_request.ticket_id)
    outcome = await runtime.vpn_reissue.start(
        request=redeploy_request,
        runtime=runtime,
        run_context=context,
    )
    if outcome.get("status") == ApprovalStatus.FAILED.value:
        raise HTTPException(
            status_code=409,
            detail={"status": outcome["status"], "fail_reasons": outcome.get("fail_reasons")},
        )
    return outcome


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


@router.post("/{operation_id}/approve")
async def approve_reissue(
    operation_id: str,
    request: Request,
    payload: ApprovalDecision,
    principal: Principal = Depends(rate_limit_dependency),
):
    """审批通过：经 VpnReissueService.approve 受控执行并落终态，附带 domain APPROVE 迁移。

    请求体仅 {operation_id, decision}；请求内容由服务从 store 的 request_snapshot 重建。
    """
    _require_scope(principal, "ticket:approve")
    runtime = _runtime(request)
    if payload.operation_id != operation_id:
        raise HTTPException(status_code=409, detail="operation_id 不匹配")
    _assert_tenant(operation_id, principal)
    # 存储作用域读取：既取业务字段（ticket_id / 后续 domain 迁移用），又兜底租户隔离。
    op = await runtime.vpn_reissue.store.get_operation(
        tenant_id=principal.tenant_id, operation_id=operation_id
    )
    ticket_id = op.ticket_id if op is not None else "-"
    ctx = _run_context(principal, ticket_id=ticket_id)
    result = await runtime.vpn_reissue.approve(
        operation_id=operation_id,
        decision=payload.decision,
        approver_user_id=principal.user_id,
        runtime=runtime,
        run_context=ctx,
    )
    _raise_transition_error(result)
    req = _operation_to_request(op) if op is not None else None
    if result.ok and req is not None:
        await _domain_after_approve(runtime, req, principal)
    return result.model_dump(mode="json")


@router.post("/{operation_id}/reject")
async def reject_reissue(
    operation_id: str,
    request: Request,
    payload: ApprovalDecision,
    principal: Principal = Depends(rate_limit_dependency),
):
    """审批拒绝：从 store 重建请求并置 REJECTED + 工单回可接管态。

    请求体仅 {operation_id, decision}；请求内容由 store 的 request_snapshot 重建。
    """
    _require_scope(principal, "ticket:approve")
    runtime = _runtime(request)
    if payload.operation_id != operation_id:
        raise HTTPException(status_code=409, detail="operation_id 不匹配")
    _assert_tenant(operation_id, principal)
    op = await runtime.vpn_reissue.store.get_operation(
        tenant_id=principal.tenant_id, operation_id=operation_id
    )
    if op is None:
        raise HTTPException(status_code=404, detail="审批操作不存在")
    req = _operation_to_request(op)
    if req is None:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "missing_request_snapshot", "reason": "审批请求快照缺失"},
        )
    ctx = _run_context(principal, ticket_id=req.ticket_id)
    result = await runtime.vpn_reissue.reject(
        request=req,
        runtime=runtime,
        run_context=ctx,
        approver_user_id=principal.user_id,
        reason="审批拒绝",
    )
    _raise_transition_error(result)
    await _domain_after_reject(runtime, req, principal)
    return result.model_dump(mode="json")


@router.post("/{operation_id}/confirm-submission")
async def confirm_submission(
    operation_id: str,
    request: Request,
    payload: SubmissionConfirmation,
    principal: Principal = Depends(rate_limit_dependency),
):
    """人工确认 FMG 是否已提交；submitted 只查询 task，不重新发起 install。"""
    _require_scope(principal, "vpn:reconcile")
    runtime = _runtime(request)
    if payload.operation_id != operation_id:
        raise HTTPException(status_code=409, detail="operation_id 不匹配")
    _assert_tenant(operation_id, principal)
    op = await runtime.vpn_reissue.store.get_operation(
        tenant_id=principal.tenant_id, operation_id=operation_id
    )
    if op is None:
        raise HTTPException(status_code=404, detail="审批操作不存在")
    ctx = _run_context(principal, ticket_id=op.ticket_id)
    result = await runtime.vpn_reissue.confirm_submission(
        operation_id=operation_id,
        submission_state=payload.submission_state,
        vendor_task_id=payload.vendor_task_id,
        note=payload.note,
        confirmer_user_id=principal.user_id,
        runtime=runtime,
        run_context=ctx,
    )
    code = result.get("error_code")
    if code == "not_found":
        raise HTTPException(status_code=404, detail=result)
    if code in (
        "illegal_transition",
        "missing_request_snapshot",
        "vendor_task_id_required",
        "vendor_task_id_forbidden",
        "invalid_submission_state",
    ):
        raise HTTPException(status_code=409 if code in ("illegal_transition", "missing_request_snapshot") else 422, detail=result)
    return result


@router.post("/reconcile")
async def reconcile_reissues(
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """补偿对账（手工/worker 触发）：扫描并收敛所有 execution_unknown / reconciliation_required。

    读统一经服务层的 store.list_reconcilable（存储为唯一权威，不依赖内存登记表）。
    """
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    ctx = _run_context(principal, ticket_id="-")
    results = await runtime.vpn_reissue.reconcile_all(runtime=runtime, run_context=ctx)
    return {"reconciled": True, "results": results}


@router.post("/{operation_id}/reconcile")
async def reconcile_reissue(
    operation_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """对某个操作执行补偿对账（收敛 execution_unknown / reconciliation_required）。

    operation_id 内嵌租户需与当前主体一致（防跨租户对账）；读经服务层的 store 读取。
    """
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    _assert_tenant(operation_id, principal)
    ctx = _run_context(principal, ticket_id="-")
    return await runtime.vpn_reissue.reconcile(
        idempotency_key=operation_id, runtime=runtime, run_context=ctx
    )


@router.get("/{operation_id}")
async def get_reissue(
    operation_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """从 store 查询某次重新下发配置的审批/执行结果（operation_id 即幂等键）。"""
    _require_scope(principal, "ticket:agent")
    runtime = _runtime(request)
    _assert_tenant(operation_id, principal)
    op = await runtime.vpn_reissue.store.get_operation(
        tenant_id=principal.tenant_id, operation_id=operation_id
    )
    if op is None:
        raise HTTPException(status_code=404, detail="审批操作不存在")
    existing_result = _operation_result(op)
    return {
        "operation_id": op.operation_id,
        "status": op.status.value,
        "result": existing_result.model_dump(mode="json") if existing_result else None,
        "request_snapshot": dict(op.request_snapshot),
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
