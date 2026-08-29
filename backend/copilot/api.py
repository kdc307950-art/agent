"""Resolution Copilot HTTP API（工单详情内的"生成处理建议"）。

职责：
    - POST /tickets/{ticket_id}/copilot：生成建议（operation_id 幂等）
    - GET  /tickets/{ticket_id}/copilot/latest：查询最新草稿
    - POST /tickets/{ticket_id}/copilot/{draft_id}/approve：审批草稿

安全原则：
    - 只允许 ticket:agent scope（坐席工作台；客户不可触发 Copilot）
    - 只读执行：Copilot 不改工单状态、不写消息、不触发 Outbox
    - 草稿审批只是状态迁移（generated -> approved），不代表发送消息；
      实际发送仍由客服在界面上确认后走既有 send_message 流程
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..security import Principal, rate_limit_dependency
from .service import CopilotService

copilot_router = APIRouter(prefix="/tickets", tags=["copilot"])


class CopilotGenerateRequest(BaseModel):
    """生成 Copilot 建议的请求：operation_id 保证幂等，expected_version 并发校验。"""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    expected_version: int = Field(ge=0)


class CopilotApproveRequest(BaseModel):
    """审批草稿：可附带审批说明（只记录，不执行发送）。"""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2_000)


def _copilot_runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not hasattr(runtime, "copilot"):
        raise HTTPException(status_code=503, detail="Copilot 服务尚未初始化")
    return runtime


@copilot_router.post("/{ticket_id}/copilot")
async def generate_copilot(
    ticket_id: str,
    payload: CopilotGenerateRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """生成 Resolution Copilot 处理建议。

    流程（PRD 第七节）：
        1. 校验租户与坐席权限
        2. 校验工单状态为 assigned / in_progress
        3. operation_id 幂等：已生成过则直接返回已有结果
        4. 读取工单/资产/消息/历史上下文
        5. 执行 Copilot Agent（有界只读工具循环）
        6. 运行引用与敏感信息门禁
        7. 保存草稿与运行审计
        8. 返回结构化结果

    模型失败不改变工单状态、不影响 SLA、不创建客户消息，返回 502 可重试错误。
    """
    _require_copilot_scope(principal)
    runtime = _copilot_runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status.value not in {"assigned", "in_progress"}:
        raise HTTPException(
            status_code=409,
            detail=f"工单状态 {ticket.status.value} 不支持生成处理建议（仅 assigned/in_progress）",
        )
    if payload.expected_version != ticket.version:
        # 版本已变：工单可能被并发修改；不重试生成，要求刷新后重试
        raise HTTPException(status_code=409, detail="工单版本已变化，请刷新后重试")

    # operation_id 幂等：已成功生成过的 operation 直接返回，不重复消耗模型
    existing_run = await runtime.copilot_repository.get_run_by_operation(
        principal.tenant_id, ticket_id, payload.operation_id
    )
    if existing_run is not None:
        draft = await runtime.copilot_repository.get_latest_draft(
            principal.tenant_id, ticket_id
        )
        return {
            "run_id": existing_run["run_id"],
            "draft": draft,
            "idempotent_replay": True,
        }

    run_id = uuid4().hex
    created = await runtime.copilot_repository.start_run(
        run_id=run_id,
        tenant_id=principal.tenant_id,
        ticket_id=ticket_id,
        operation_id=payload.operation_id,
    )
    if not created:
        # 并发下另一请求已登记同一 operation：返回已有草稿
        draft = await runtime.copilot_repository.get_latest_draft(
            principal.tenant_id, ticket_id
        )
        return {"run_id": run_id, "draft": draft, "idempotent_replay": True}

    import time

    service: CopilotService = runtime.copilot
    metrics = getattr(runtime, "metrics", None)
    started = time.perf_counter()
    try:
        # 完整流程：准备上下文 -> 检索引用白名单 -> 生成 -> 门禁
        outcome = await service.run_with_tenant(
            runtime=runtime,
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
        )
        gated = outcome["result"]
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        if metrics is not None:
            metrics.increment("copilot_failures_total", attributes={"error_code": "generation_exception"})
            metrics.observe("copilot_latency_seconds", latency_ms / 1000)
        await runtime.copilot_repository.finish_run(
            run_id=run_id,
            tenant_id=principal.tenant_id,
            status="failed",
            tool_calls=0,
            latency_ms=latency_ms,
            error_code="copilot_generation_failed",
        )
        raise HTTPException(status_code=502, detail="处理建议生成失败，请稍后重试") from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    status = "completed" if gated.error_code is None else "failed"
    if metrics is not None:
        metrics.increment("copilot_runs_total", attributes={"status": status})
        metrics.increment(
            "copilot_failures_total",
            attributes={"error_code": gated.error_code or "none"},
        )
        metrics.observe("copilot_latency_seconds", latency_ms / 1000)
        if gated.needs_human_review:
            metrics.increment("copilot_human_revision_total")
        if not gated.citations:
            metrics.increment("copilot_no_citation_total")
    await runtime.copilot_repository.finish_run(
        run_id=run_id,
        tenant_id=principal.tenant_id,
        status=status,
        tool_calls=len(gated.tool_trace),
        latency_ms=latency_ms,
        error_code=gated.error_code,
    )
    draft_id = uuid4().hex
    await runtime.copilot_repository.save_draft(
        draft_id=draft_id,
        tenant_id=principal.tenant_id,
        ticket_id=ticket_id,
        run_id=run_id,
        draft_answer=gated.draft_answer,
        steps=gated.troubleshooting_steps,
        citations=[c.model_dump(mode="json") for c in gated.citations],
        confidence=gated.confidence,
        needs_human_review=gated.needs_human_review,
    )
    return {
        "run_id": run_id,
        "draft": await runtime.copilot_repository.get_latest_draft(
            principal.tenant_id, ticket_id
        ),
        "idempotent_replay": False,
    }


@copilot_router.get("/{ticket_id}/copilot/latest")
async def get_copilot_latest(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """查询工单最新 Copilot 草稿（无则返回 null）。"""
    _require_copilot_scope(principal)
    runtime = _copilot_runtime(request)
    draft = await runtime.copilot_repository.get_latest_draft(
        principal.tenant_id, ticket_id
    )
    return {"draft": draft}


@copilot_router.post("/{ticket_id}/copilot/{draft_id}/approve")
async def approve_copilot_draft(
    ticket_id: str,
    draft_id: str,
    payload: CopilotApproveRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """审批 Copilot 草稿（generated -> approved）。

    审批只表示客服采纳该草稿，不执行发送；消息发送仍走既有流程。
    """
    _require_copilot_scope(principal)
    runtime = _copilot_runtime(request)
    ticket = await runtime.tickets.get(principal.tenant_id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    approved = await runtime.copilot_repository.approve_draft(
        tenant_id=principal.tenant_id, draft_id=draft_id, approved_by=principal.user_id
    )
    if not approved:
        raise HTTPException(status_code=409, detail="草稿不存在或已处理")
    metrics = getattr(runtime, "metrics", None)
    if metrics is not None:
        metrics.increment("copilot_draft_approval_total")
    audit = getattr(runtime, "audit", None)
    if audit is not None:
        try:
            await audit.record_admin_event(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                action="copilot_draft.approve",
                resource_type="copilot_draft",
                resource_id=draft_id,
                detail={"ticket_id": ticket_id, "note": payload.note},
            )
        except Exception:
            pass
    return {"draft_id": draft_id, "status": "approved"}


def _require_copilot_scope(principal: Principal) -> None:
    if "ticket:agent" not in principal.scopes:
        raise HTTPException(status_code=403, detail="缺少 ticket:agent 权限")
