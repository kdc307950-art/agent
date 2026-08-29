"""Resolution Copilot HTTP API（工单详情内的"生成处理建议"）。

职责：
    - POST /tickets/{ticket_id}/copilot：提交生成任务（异步 Worker 化：入队返回 202）
    - GET  /tickets/{ticket_id}/copilot/{run_id}：查询运行状态（前端轮询）
    - GET  /tickets/{ticket_id}/copilot/latest：查询最新草稿（展示用）
    - POST /tickets/{ticket_id}/copilot/{draft_id}/approve：审批草稿

安全原则：
    - 只允许 ticket:agent scope（坐席工作台；客户不可触发 Copilot）
    - 只读执行：Copilot 不改工单状态、不写消息、不触发 Outbox
    - 草稿审批只是状态迁移（generated -> approved），不代表发送消息；
      实际发送仍由客服在界面上确认后走既有 send_message 流程
    - 模型执行在 CopilotWorker 进程内完成，Web 进程不调模型
    - 运行状态机（阶段二）：
        completed + 对应 draft -> 返回该 run 的 draft（不跨 run 取最新）
        queued/processing       -> 202 {"status", "run_id"}
        failed                  -> 允许新 operation_id 重试（不当作成功幂等）
        dead                    -> 转人工；expired -> 允许重新入队
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..security import Principal, rate_limit_dependency

copilot_router = APIRouter(prefix="/tickets", tags=["copilot"])

# 运行租约（秒）：超过则视为僵尸运行，由 Worker recover 回队
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


@copilot_router.post("/{ticket_id}/copilot")
async def generate_copilot(
    ticket_id: str,
    payload: CopilotGenerateRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """提交 Copilot 生成任务（阶段二：异步 Worker 化）。

    流程：
        1. 校验租户与坐席权限；服务未初始化返回 503
        2. 校验工单状态为 assigned / in_progress（否则 409）
        3. operation_id 幂等：
           - completed -> 按该 run_id 查 draft 返回（不跨 run 取最新）
           - queued/processing -> 202 {"status", "run_id"}（不重复消耗模型）
           - failed -> 409 要求新 operation_id；dead -> 409 转人工
           - expired -> 允许同一 operation 重新入队
        4. 创建 queued 运行，立即返回 202
        5. 模型执行由 CopilotWorker 领取后异步完成（Web 进程不调模型）
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

    # 僵尸运行恢复由 CopilotWorker 定期执行（阶段六），HTTP 请求不承担恢复职责

    # operation_id 幂等：按 run 状态分派（阶段二异步 Worker 状态机）
    existing_run = await runtime.copilot_repository.get_run_by_operation(
        principal.tenant_id, ticket_id, payload.operation_id
    )
    if existing_run is not None:
        if existing_run["status"] in {"queued", "processing"}:
            # 已入队/处理中：返回 202，前端轮询任务状态，不重复调用模型
            return JSONResponse(
                status_code=202,
                content={
                    "status": existing_run["status"],
                    "run_id": existing_run["run_id"],
                },
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
        if existing_run["status"] == "dead":
            # 死信：必须转人工处理，不允许静默重跑
            raise HTTPException(
                status_code=409,
                detail="上次 Copilot 生成进入死信，请转人工处理或联系管理员",
            )
        # expired（僵尸运行租约过期）：允许同一 operation 重新运行，
        # 由下方 start_run 的 ON CONFLICT 分支把 expired 重置为 queued

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
        if race is not None and race["status"] in {"queued", "processing"}:
            return JSONResponse(
                status_code=202,
                content={"status": race["status"], "run_id": race["run_id"]},
            )
        if race is not None and race["status"] == "completed":
            draft = await runtime.copilot_repository.get_draft_by_run(
                principal.tenant_id, ticket_id, race["run_id"]
            )
            return {"run_id": race["run_id"], "draft": draft, "idempotent_replay": True}
        raise HTTPException(status_code=409, detail="Copilot 运行登记冲突，请重试")

    # 异步 Worker 化（阶段二）：POST 只入队立即返回 202，
    # 模型执行由 CopilotWorker 领取后完成（Web 进程不调模型）
    metrics = getattr(runtime, "metrics", None)
    if metrics is not None:
        metrics.increment("copilot_runs_total", attributes={"status": "queued"})
    return JSONResponse(
        status_code=202,
        content={"status": "queued", "run_id": run_id},
    )


@copilot_router.get("/{ticket_id}/copilot/{run_id}")
async def get_copilot_run_status(
    ticket_id: str,
    run_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """查询 Copilot 运行状态（前端轮询；阶段二异步 Worker）。

    返回：
        {"run_id", "status", "draft_id", "error_code", "tool_calls"}
    status: queued / processing / completed / failed / dead / expired
    completed 时 draft_id 指向对应草稿（按 run_id 关联，不跨 run）。
    """
    _require_copilot_scope(principal)
    # 守卫：run_id == "latest" 时交给 latest 语义（该路径先于静态路由被捕获）
    if run_id == "latest":
        runtime = _copilot_runtime(request)
        draft = await runtime.copilot_repository.get_latest_draft(
            principal.tenant_id, ticket_id
        )
        return {"draft": draft}
    runtime = _copilot_runtime(request)
    run = await runtime.copilot_repository.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    draft = None
    if run["status"] == "completed":
        draft = await runtime.copilot_repository.get_draft_by_run(
            principal.tenant_id, ticket_id, run_id
        )
    return {
        "run_id": run_id,
        "status": run["status"],
        "draft": draft,
        "draft_id": draft["draft_id"] if draft else None,
        "error_code": run.get("error_code"),
        "tool_calls": run.get("tool_calls") or 0,
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
