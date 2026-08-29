"""Resolution Copilot HTTP API（工单详情内的"生成处理建议"）。

职责：
    - POST /tickets/{ticket_id}/copilot：生成建议（operation_id 幂等 + 运行状态机）
    - GET  /tickets/{ticket_id}/copilot/latest：查询最新草稿
    - POST /tickets/{ticket_id}/copilot/{draft_id}/approve：审批草稿

安全原则：
    - 只允许 ticket:agent scope（坐席工作台；客户不可触发 Copilot）
    - 只读执行：Copilot 不改工单状态、不写消息、不触发 Outbox
    - 草稿审批只是状态迁移（generated -> approved），不代表发送消息；
      实际发送仍由客服在界面上确认后走既有 send_message 流程
    - 运行状态机（阶段三）：
        completed + 对应 draft -> 返回该 run 的 draft（不跨 run 取最新）
        running                 -> 202 {"status": "running", "run_id"}
        failed                  -> 允许新 operation_id 重试（不当作成功幂等）
        超租约僵尸运行          -> recover_expired_runs 标记 failed 后可重试
"""

from __future__ import annotations

from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..run_context import RunContext
from ..security import Principal, rate_limit_dependency
from .service import CopilotService

copilot_router = APIRouter(prefix="/tickets", tags=["copilot"])

# 运行租约（秒）：超过则视为僵尸运行，recover 后允许重试
RUN_LEASE_SECONDS = 60


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
    """取 Copilot 运行时；服务未初始化（无模型 key / 未装配）返回 503。

    与 502 的区分：503 = 服务不可用（未配置），502 = 服务在但模型调用失败。
    """
    runtime = getattr(request.app.state, "runtime", None)
    if (
        runtime is None
        or getattr(runtime, "copilot", None) is None
        or getattr(runtime, "copilot_repository", None) is None
    ):
        raise HTTPException(status_code=503, detail="Copilot 服务尚未初始化（未配置模型服务）")
    return runtime


def _build_run_context(
    request: Request,
    principal: Principal,
    *,
    run_id: str,
    ticket_id: str,
) -> RunContext:
    """构造 Copilot 工具执行的服务端 RunContext（阶段一：工具治理上下文）。

    来源只能是服务端 principal（token 解析结果）与 settings，绝不来自请求体。
    allowed_tools 限定为 Copilot 只读 profile，治理层据此拒绝副作用工具。
    """
    settings = getattr(request.app.state, "settings", None)
    timeout_seconds = getattr(settings, "agent_run_timeout_seconds", 60) or 60
    from .tool_adapter import COPILOT_ALLOWED_TOOLS

    return RunContext(
        run_id=run_id,
        request_id=getattr(request.state, "request_id", "") or uuid4().hex,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        thread_id=f"copilot:{principal.tenant_id}:{ticket_id}",
        scopes=frozenset(principal.scopes),
        deadline=monotonic() + timeout_seconds,
        allowed_tools=frozenset(COPILOT_ALLOWED_TOOLS),
    )


@copilot_router.post("/{ticket_id}/copilot")
async def generate_copilot(
    ticket_id: str,
    payload: CopilotGenerateRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """生成 Resolution Copilot 处理建议（含运行状态机与幂等恢复）。

    流程（PRD 第七节 + 阶段三）：
        1. 校验租户与坐席权限；服务未初始化返回 503
        2. 校验工单状态为 assigned / in_progress（否则 409）
        3. operation_id 幂等：
           - completed -> 按该 run_id 查 draft 返回（不跨 run 取最新）
           - running   -> 202 {"status": "running", "run_id"}（不重复消耗模型）
           - failed    -> 允许客户端用新 operation_id 显式重试
        4. 先 recover 超租约僵尸运行，再读取工单上下文
        5. 执行 Copilot Agent（治理层包装的只读工具循环）
        6. 运行引用与敏感信息门禁（基于实际工具证据）
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

    # 先清理超租约僵尸运行（进程崩溃后遗留的 running）
    try:
        await runtime.copilot_repository.recover_expired_runs(
            lease_seconds=RUN_LEASE_SECONDS
        )
    except Exception:
        pass  # 恢复失败不阻塞主流程（下一轮再试）

    # operation_id 幂等：按 run 状态分派（阶段三）
    existing_run = await runtime.copilot_repository.get_run_by_operation(
        principal.tenant_id, ticket_id, payload.operation_id
    )
    if existing_run is not None:
        if existing_run["status"] == "running":
            # 仍在执行：返回 202，前端轮询任务状态，不重复调用模型
            return JSONResponse(
                status_code=202,
                content={"status": "running", "run_id": existing_run["run_id"]},
            )
        if existing_run["status"] == "completed":
            # 已完成：按该 run_id 查草稿（绝不跨 run 取最新草稿）
            draft = await runtime.copilot_repository.get_draft_by_run(
                principal.tenant_id, ticket_id, existing_run["run_id"]
            )
            return {
                "run_id": existing_run["run_id"],
                "draft": draft,
                "idempotent_replay": True,
            }
        if existing_run["status"] == "failed":
            # 失败运行：不能当成功幂等结果；返回原错误，允许新 operation_id 重试
            raise HTTPException(
                status_code=409,
                detail=(
                    f"上次 Copilot 生成失败（{existing_run.get('error_code') or 'unknown'}），"
                    "请使用新的 operation_id 重试"
                ),
            )
        # expired（僵尸运行租约过期）：允许同一 operation 重新运行，
        # 由下方 start_run 的 ON CONFLICT 分支把 expired 重置为 running

    run_id = uuid4().hex
    created = await runtime.copilot_repository.start_run(
        run_id=run_id,
        tenant_id=principal.tenant_id,
        ticket_id=ticket_id,
        operation_id=payload.operation_id,
        lease_seconds=RUN_LEASE_SECONDS,
    )
    if not created:
        # 并发下另一请求已登记同一 operation：按其状态分派
        race = await runtime.copilot_repository.get_run_by_operation(
            principal.tenant_id, ticket_id, payload.operation_id
        )
        if race is not None and race["status"] == "running":
            return JSONResponse(
                status_code=202,
                content={"status": "running", "run_id": race["run_id"]},
            )
        if race is not None and race["status"] == "completed":
            draft = await runtime.copilot_repository.get_draft_by_run(
                principal.tenant_id, ticket_id, race["run_id"]
            )
            return {"run_id": race["run_id"], "draft": draft, "idempotent_replay": True}
        raise HTTPException(status_code=409, detail="Copilot 运行登记冲突，请重试")

    service: CopilotService = runtime.copilot
    metrics = getattr(runtime, "metrics", None)
    run_context = _build_run_context(request, principal, run_id=run_id, ticket_id=ticket_id)
    started = monotonic()
    try:
        # 完整流程：准备上下文 -> 生成（治理工具）-> 按实际工具证据门禁
        outcome = await service.run_with_tenant(
            runtime=runtime,
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            run_context=run_context,
        )
        gated = outcome["result"]
    except Exception as exc:
        latency_ms = int((monotonic() - started) * 1000)
        if metrics is not None:
            metrics.increment(
                "copilot_failures_total", attributes={"error_code": "generation_exception"}
            )
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

    latency_ms = int((monotonic() - started) * 1000)
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
        "draft": await runtime.copilot_repository.get_draft_by_run(
            principal.tenant_id, ticket_id, run_id
        ),
        "idempotent_replay": False,
    }


@copilot_router.get("/{ticket_id}/copilot/latest")
async def get_copilot_latest(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """查询工单最新 Copilot 草稿（无则返回 null）。

    注意：latest 用于展示；幂等恢复必须用 run_id 对应的 draft（get_draft_by_run）。
    """
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
