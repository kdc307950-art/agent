"""FastAPI 主应用 —— Agent 的 HTTP 网关。

职责：
    - 提供 /api/chat/stream 流式对话接口（单 Agent / Supervisor / JSON 工作流三种图形态）
    - /api/chat/resume 恢复被 human_approval 挂起的审批（interrupt_id 防重复/防串批）
    - 鉴权（dev token / OIDC）、租户隔离（thread_id 加租户命名空间）
    - 限流（内存 / Redis）、审计、用量计量、预算控制、健康检查、metrics

关键设计：
    - lifespan 里装配 runtime_context（Postgres checkpointer + Redis + 治理等）
    - _execute_run 统一处理三种图的执行，并把 interrupt 通过 SSE 事件下发
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from time import monotonic, perf_counter
from typing import Literal
from uuid import uuid4

import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from .assets.api import router as asset_router
from .audit import NoopAuditRepository
from .budget import TenantBudget, TenantBudgetExceeded
from .config import load_environment
from .knowledge.api import router as knowledge_router
from .metrics import RuntimeMetrics
from .rate_limit import RedisRateLimiter
from .readiness import probe_dependencies
from .repositories import tenant_thread_id
from .revocation import RedisRevocationStore
from .run_context import RunContext
from .runtime import runtime_context
from .security import (
    InMemoryRateLimiter,
    OIDCVerifier,
    Principal,
    authenticate,
    cors_origins,
    rate_limit_dependency,
)
from .settings import Settings
from .telemetry import Telemetry
from .ticket_api import admin_router, channel_router
from .ticket_api import router as ticket_router
from .usage import extract_model_usage, usage_cost_usd
from .worker_metrics import WorkerMetricsDB, prometheus_text

logger = logging.getLogger("langgraph.api")


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=4_000)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content 不能只包含空白字符")
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4_000)
    thread_id: str = Field(
        default="user_web_001",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    history: list[HistoryMessage] = Field(default_factory=list, max_length=50)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message 不能只包含空白字符")
        return value


class ResumeRequest(BaseModel):
    """恢复一次被 human_approval 节点挂起的运行。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(
        default="user_web_001",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    approved: bool
    # 由 interrupt 事件下发，用于防重复审批与防串批；省略则只校验「存在挂起审批」
    interrupt_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    # 挂起那一轮的 run_id，客户端原样回传，仅用于审计串联展示（不作为授权依据）
    resumed_from: str | None = Field(
        default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9]+$"
    )


class RevokeTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jti: str = Field(min_length=8, max_length=256, pattern=r"^[A-Za-z0-9._:-]+$")
    expires_at: int = Field(gt=0)


# LangGraph 在 stream_mode="updates" 下用这个保留 key 下发挂起的 interrupt，
# 它不是业务节点名，需要单独分支处理。
INTERRUPT_CHANNEL = "__interrupt__"


def _iter_messages(update: object) -> list[object]:
    """从一次节点更新里取出消息列表。

    不依赖节点名字：单 Agent 图的节点叫 agent/tools，编排图的节点叫
    supervisor/approval/weather_agent…… 靠名字判断会让计量和审计在编排图下
    静默失效，所以这里只看更新的结构。
    """
    if not isinstance(update, dict):
        return []
    messages = update.get("messages")
    if messages is None:
        return []
    if isinstance(messages, (list, tuple)):
        return list(messages)
    return [messages]


def _pending_interrupts(snapshot: object) -> list[object]:
    """展开状态快照里所有挂起的 interrupt。"""
    pending: list[object] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            pending.append(item)
    return pending


def _interrupt_question(item: object) -> str:
    """取出要展示给审批人的问题文本。

    节点通过 `interrupt({"question": ...})` 挂起，但 value 的形状由业务节点决定，
    这里对非 dict 的 value 做兜底，避免审批卡片因为节点写法不同而空白。
    """
    value = getattr(item, "value", None)
    if isinstance(value, dict):
        return str(value.get("question") or value)
    return str(value) if value is not None else "是否批准继续执行？"


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = request_id
        started = perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            status_code = response.status_code if response is not None else 500
            logger.info(
                "request_id=%s run_id=%s tenant_hash=%s method=%s path=%s status=%s client=%s elapsed_ms=%.1f",
                request_id,
                getattr(request.state, "run_id", ""),
                getattr(request.state, "tenant_hash", ""),
                request.method,
                request.url.path,
                status_code,
                request.client.host if request.client else "unknown",
                elapsed_ms,
            )
            if hasattr(request.app.state, "metrics"):
                request.app.state.metrics.increment("http_requests_total")
                route = request.url.path
                if route.startswith("/audit/runs/"):
                    route = "/audit/runs/{run_id}"
                request.app.state.metrics.observe(
                    "agent_http_duration_seconds",
                    elapsed_ms / 1000,
                    {
                        "method": request.method,
                        "route": route,
                        "status_code": status_code,
                    },
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_environment()  # 进程入口统一加载 .env（幂等，不覆盖已注入变量）
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.metrics = RuntimeMetrics()
    app.state.telemetry = Telemetry(app)
    app.state.audit = NoopAuditRepository()
    app.state.revocation_store = None
    app.state.redis_client = None
    app.state.budget = None
    redis_client = None
    auth_verifier = None
    try:
        if settings.rate_limit_backend == "redis" or (
            settings.auth_mode == "oidc" and settings.oidc_revocation_mode == "redis"
        ):
            redis_client = redis.from_url(
                settings.redis_url or "",
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=settings.redis_socket_timeout_seconds,
                socket_connect_timeout=settings.redis_socket_timeout_seconds,
            )
            try:
                await redis_client.ping()
            except Exception:
                await redis_client.aclose()
                if settings.redis_fail_mode == "closed":
                    raise RuntimeError("Redis 不可用，按 fail-closed 策略拒绝启动") from None
                redis_client = None
            app.state.redis_client = redis_client

        if settings.tenant_daily_budget_usd > 0:
            if redis_client is None:
                raise RuntimeError("租户预算需要可用 Redis")
            app.state.budget = TenantBudget(
                redis_client,
                daily_limit_usd=settings.tenant_daily_budget_usd,
            )

        app.state.memory_rate_limiter = InMemoryRateLimiter(settings.rate_limit_capacity)
        if settings.rate_limit_backend == "redis" and redis_client is not None:
            app.state.rate_limiter = RedisRateLimiter(
                redis_client,
                capacity=settings.rate_limit_capacity,
                refill_per_second=settings.rate_limit_refill_per_second,
            )
        else:
            app.state.rate_limiter = InMemoryRateLimiter(settings.rate_limit_capacity)

        if settings.auth_mode == "oidc":
            revocation_store = (
                RedisRevocationStore(redis_client)
                if settings.oidc_revocation_mode == "redis" and redis_client
                else None
            )
            if settings.oidc_revocation_mode == "redis" and revocation_store is None:
                raise RuntimeError("OIDC_REVOCATION_MODE=redis 但 Redis 不可用")
            app.state.revocation_store = revocation_store
            auth_verifier = OIDCVerifier(
                issuer=settings.oidc_issuer_url or "",
                audience=settings.oidc_audience or "",
                jwks_url=settings.oidc_jwks_url,
                tenant_claim=settings.oidc_tenant_claim,
                clock_skew_seconds=settings.oidc_clock_skew_seconds,
                required_scopes=settings.oidc_required_scopes,
                revocation_store=revocation_store,
                cache_seconds=settings.oidc_jwks_cache_seconds,
                require_jti=settings.oidc_require_jti,
                max_token_age_seconds=settings.oidc_max_token_age_seconds,
            )
        app.state.auth_verifier = auth_verifier
        logger.info(
            "正在初始化 LangGraph Agent runtime auth_mode=%s rate_limit_backend=%s",
            settings.auth_mode,
            settings.rate_limit_backend,
        )
        try:
            runtime_manager = runtime_context(settings, metrics=app.state.metrics)
        except TypeError:
            # Keep lightweight test fixtures compatible with the production signature.
            runtime_manager = runtime_context(settings)
        async with runtime_manager as runtime:
            app.state.runtime = runtime
            app.state.agent = runtime.graph
            app.state.audit = getattr(runtime, "audit", NoopAuditRepository())
            logger.info("LangGraph Agent runtime 初始化完成")
            yield
    finally:
        if auth_verifier is not None:
            await auth_verifier.aclose()
        if redis_client is not None:
            await redis_client.aclose()
        app.state.agent = None
        app.state.runtime = None
        app.state.audit = NoopAuditRepository()
        app.state.revocation_store = None
        app.state.redis_client = None
        app.state.budget = None
        if hasattr(app.state, "metrics"):
            app.state.metrics.shutdown()
        if hasattr(app.state, "telemetry"):
            app.state.telemetry.shutdown()


app = FastAPI(title="LangGraph Agent API", lifespan=lifespan)
app.include_router(ticket_router)
app.include_router(channel_router)
app.include_router(admin_router)
app.include_router(asset_router)
app.include_router(knowledge_router)
app.add_middleware(AuditMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


async def _execute_run(
    *,
    http_request: Request,
    principal: Principal,
    thread_id: str,
    inputs: object,
    extra_metadata: dict[str, object] | None = None,
) -> StreamingResponse:
    """执行一次图运行并以 SSE 返回。

    /chat/stream（新消息）与 /chat/resume（审批恢复）共用这条路径，因此租户隔离、
    预算、审计、超时、指标对两者完全一致，差别只在 inputs 是消息还是 Command(resume=...)。
    """
    graph = http_request.app.state.agent
    if graph is None:
        raise HTTPException(status_code=503, detail="Agent 尚未初始化")

    run_id = uuid4().hex
    run_started = monotonic()
    http_request.state.run_id = run_id
    http_request.state.tenant_hash = hashlib.sha256(
        principal.tenant_id.encode("utf-8")
    ).hexdigest()[:16]
    http_request.app.state.metrics.increment("agent_runs_total")
    physical_thread_id = tenant_thread_id(principal.tenant_id, principal.user_id, thread_id)
    config = {"configurable": {"thread_id": physical_thread_id, "checkpoint_ns": ""}}
    settings = http_request.app.state.settings
    run_context = RunContext(
        run_id=run_id,
        request_id=http_request.state.request_id,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        thread_id=physical_thread_id,
        scopes=principal.scopes,
        deadline=monotonic() + settings.agent_run_timeout_seconds,
    )
    budget = getattr(http_request.app.state, "budget", None)
    if budget is not None:
        try:
            if not await budget.can_start(principal.tenant_id):
                http_request.app.state.metrics.increment("tenant_budget_rejections_total")
                raise HTTPException(status_code=429, detail="租户当日模型预算已用尽")
        except HTTPException:
            raise
        except Exception as exc:
            http_request.app.state.metrics.increment("budget_errors_total")
            raise HTTPException(status_code=503, detail="租户预算服务暂时不可用") from exc
    audit = getattr(http_request.app.state, "audit", NoopAuditRepository())
    try:
        async with asyncio.timeout(settings.audit_write_timeout_seconds):
            await audit.start_run(
                run_context,
                metadata={"route": http_request.url.path, **(extra_metadata or {})},
            )
    except Exception as exc:
        http_request.app.state.metrics.increment("audit_errors_total")
        logger.exception(
            "无法创建运行审计记录 request_id=%s run_id=%s", run_context.request_id, run_id
        )
        raise HTTPException(status_code=503, detail="运行审计服务暂时不可用") from exc

    usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    total_cost_usd = 0.0

    def run_metadata() -> dict[str, object]:
        metadata: dict[str, object] = {"route": http_request.url.path, **(extra_metadata or {})}
        if usage_totals["total_tokens"]:
            metadata["model"] = settings.llm_model
            metadata["input_tokens"] = usage_totals["input_tokens"]
            metadata["output_tokens"] = usage_totals["output_tokens"]
            metadata["total_tokens"] = usage_totals["total_tokens"]
            metadata["cost_usd"] = round(total_cost_usd, 8)
        return metadata

    async def finish_run(status: str, *, error_code: str | None = None) -> None:
        try:
            async with asyncio.timeout(settings.audit_write_timeout_seconds):
                await audit.finish_run(
                    run_context,
                    status,
                    error_code=error_code,
                    metadata=run_metadata(),
                )
            http_request.app.state.metrics.observe(
                "agent_run_duration_seconds",
                monotonic() - run_started,
                {"outcome": status},
            )
        except Exception:
            http_request.app.state.metrics.increment("audit_errors_total")
            logger.exception(
                "无法更新运行审计状态 request_id=%s run_id=%s status=%s",
                run_context.request_id,
                run_id,
                status,
            )

    async def event_generator():
        nonlocal total_cost_usd
        interrupted = False
        try:
            async with asyncio.timeout(settings.agent_run_timeout_seconds):
                async for event in graph.astream(
                    inputs,
                    config=config,
                    context=run_context,
                    stream_mode="updates",
                ):
                    for node_name, update in event.items():
                        # __interrupt__ 是 LangGraph 的保留通道，不是业务节点
                        if node_name == INTERRUPT_CHANNEL:
                            for item in update or ():
                                interrupted = True
                                interrupt_id = str(getattr(item, "id", "") or "")
                                question = _interrupt_question(item)
                                try:
                                    async with asyncio.timeout(
                                        settings.audit_write_timeout_seconds
                                    ):
                                        await audit.record_event(
                                            run_context,
                                            "interrupt_raised",
                                            status="pending",
                                            payload={
                                                "interrupt_id": interrupt_id,
                                                "question": question,
                                            },
                                        )
                                except Exception:
                                    http_request.app.state.metrics.increment("audit_errors_total")
                                    logger.exception("审批挂起审计写入失败 run_id=%s", run_id)
                                # 回传逻辑 thread_id（不是 physical），physical 含租户/用户信息不外泄
                                yield f"data: {json.dumps({'type': 'interrupt', 'run_id': run_id, 'thread_id': thread_id, 'interrupt_id': interrupt_id, 'question': question}, ensure_ascii=False)}\n\n"
                            continue
                        # 按消息结构而不是节点名识别：编排图的节点叫 supervisor/weather_agent……
                        # 靠名字判断会让计量、成本、预算、审计在编排图下静默失效。
                        for msg in _iter_messages(update):
                            if isinstance(msg, ToolMessage):
                                yield f"data: {json.dumps({'type': 'tool', 'status': 'done'}, ensure_ascii=False)}\n\n"
                                continue
                            if not isinstance(msg, AIMessage):
                                continue
                            usage = extract_model_usage(msg)
                            if usage.known:
                                usage_totals["input_tokens"] += usage.input_tokens
                                usage_totals["output_tokens"] += usage.output_tokens
                                usage_totals["total_tokens"] += usage.total_tokens
                                cost = usage_cost_usd(
                                    usage,
                                    input_per_1k=settings.model_input_cost_per_1k_usd,
                                    output_per_1k=settings.model_output_cost_per_1k_usd,
                                )
                                total_cost_usd += cost
                                http_request.app.state.metrics.increment(
                                    "model_input_tokens_total",
                                    usage.input_tokens,
                                    {"model": settings.llm_model},
                                )
                                http_request.app.state.metrics.increment(
                                    "model_output_tokens_total",
                                    usage.output_tokens,
                                    {"model": settings.llm_model},
                                )
                                http_request.app.state.metrics.increment(
                                    "model_cost_microusd_total",
                                    int(round(cost * 1_000_000)),
                                    {"model": settings.llm_model},
                                )
                                if budget is not None and not await budget.record(
                                    principal.tenant_id, cost
                                ):
                                    raise TenantBudgetExceeded("tenant daily model budget exceeded")
                                try:
                                    async with asyncio.timeout(
                                        settings.audit_write_timeout_seconds
                                    ):
                                        await audit.record_event(
                                            run_context,
                                            "model_usage",
                                            status="completed",
                                            payload={
                                                "model": settings.llm_model,
                                                "input_tokens": usage.input_tokens,
                                                "output_tokens": usage.output_tokens,
                                                "total_tokens": usage.total_tokens,
                                                "cost_usd": cost,
                                            },
                                        )
                                except Exception:
                                    http_request.app.state.metrics.increment("audit_errors_total")
                                    logger.exception("模型用量审计写入失败 run_id=%s", run_id)
                            if getattr(msg, "content", None):
                                yield f"data: {json.dumps({'type': 'text', 'content': msg.content}, ensure_ascii=False)}\n\n"
                            if getattr(msg, "tool_calls", None):
                                yield f"data: {json.dumps({'type': 'tool', 'status': 'calling'}, ensure_ascii=False)}\n\n"
                if interrupted:
                    # 图停在审批点：这一轮到此为止，但不是 completed，
                    # 也不发 end —— 前端要靠 end 的缺席来区分「答完了」和「等你批」。
                    await finish_run("awaiting_approval")
                    http_request.app.state.metrics.increment("agent_runs_awaiting_approval_total")
                else:
                    await finish_run("completed")
                    http_request.app.state.metrics.increment("agent_runs_completed_total")
                    yield f"data: {json.dumps({'type': 'end', 'run_id': run_id}, ensure_ascii=False)}\n\n"
        except TenantBudgetExceeded:
            await finish_run("budget_exceeded", error_code="tenant_budget_exceeded")
            http_request.app.state.metrics.increment("tenant_budget_exceeded_total")
            yield f"data: {json.dumps({'type': 'error', 'code': 'tenant_budget_exceeded', 'run_id': run_id, 'content': '租户模型预算已用尽'}, ensure_ascii=False)}\n\n"
        except TimeoutError:
            await finish_run("timeout", error_code="agent_timeout")
            http_request.app.state.metrics.increment("agent_timeouts_total")
            logger.warning(
                "Agent stream timed out request_id=%s run_id=%s",
                http_request.state.request_id,
                run_id,
            )
            yield f"data: {json.dumps({'type': 'error', 'code': 'agent_timeout', 'run_id': run_id, 'content': '请求超时，请稍后重试'}, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            await finish_run("cancelled", error_code="client_cancelled")
            http_request.app.state.metrics.increment("agent_cancellations_total")
            logger.info(
                "Agent stream cancelled request_id=%s run_id=%s",
                http_request.state.request_id,
                run_id,
            )
            raise
        except Exception:
            await finish_run("failed", error_code="agent_failed")
            http_request.app.state.metrics.increment("agent_errors_total")
            logger.exception(
                "Agent stream failed request_id=%s run_id=%s", http_request.state.request_id, run_id
            )
            yield f"data: {json.dumps({'type': 'error', 'code': 'agent_failed', 'run_id': run_id, 'content': '服务暂时不可用，请稍后重试'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Request-ID": http_request.state.request_id,
            "X-Run-ID": run_id,
        },
    )


@app.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    http_request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """流式对话端点（Server-Sent Events）。"""
    if "chat:write" not in principal.scopes:
        raise HTTPException(status_code=403, detail="缺少 chat:write 权限")
    return await _execute_run(
        http_request=http_request,
        principal=principal,
        thread_id=payload.thread_id,
        inputs={"messages": [HumanMessage(content=payload.message)]},
    )


@app.post("/chat/resume")
async def chat_resume(
    payload: ResumeRequest,
    http_request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """恢复一次被 human_approval 节点挂起的运行。

    走 rate_limit_dependency 而不是裸 authenticate：恢复执行会真实触发模型调用，
    在计费和限流上与 /chat/stream 同源，所以复用同一条依赖链（含 chat:write 校验），
    再额外要求 chat:approve —— 「能发消息」不等于「能替租户批准操作」。
    """
    if "chat:write" not in principal.scopes:
        raise HTTPException(status_code=403, detail="缺少 chat:write 权限")
    if "chat:approve" not in principal.scopes:
        raise HTTPException(status_code=403, detail="缺少 chat:approve 权限")
    graph = http_request.app.state.agent
    if graph is None:
        raise HTTPException(status_code=503, detail="Agent 尚未初始化")

    # 用物理 thread_id 读状态：跨租户/跨用户拿到的是另一个命名空间，天然读不到别人的挂起审批
    physical_thread_id = tenant_thread_id(principal.tenant_id, principal.user_id, payload.thread_id)
    config = {"configurable": {"thread_id": physical_thread_id, "checkpoint_ns": ""}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:
        logger.exception("读取会话状态失败 thread=%s", payload.thread_id)
        raise HTTPException(status_code=503, detail="会话状态服务暂时不可用") from exc

    pending = _pending_interrupts(snapshot)
    if not pending:
        # 409 而不是 404：不区分「线程不存在」和「线程存在但没在等审批」，
        # 避免通过状态码探测他人 thread_id 是否存在。
        raise HTTPException(status_code=409, detail="该会话当前没有待审批的操作")
    pending_ids = {str(getattr(item, "id", "") or "") for item in pending}
    if payload.interrupt_id is not None and payload.interrupt_id not in pending_ids:
        # 防重复审批与防串批：客户端拿着过期的 interrupt_id 回来时必须失败
        raise HTTPException(status_code=409, detail="审批标识已失效，请刷新后重试")

    resumed_interrupt_id = payload.interrupt_id or next(iter(pending_ids), "")
    logger.info(
        "审批恢复 tenant=%s thread=%s approved=%s interrupt_id=%s",
        principal.tenant_id,
        payload.thread_id,
        payload.approved,
        resumed_interrupt_id,
    )
    return await _execute_run(
        http_request=http_request,
        principal=principal,
        thread_id=payload.thread_id,
        inputs=Command(resume={"approved": payload.approved}),
        # 审计要能回答「谁在什么时候批准了什么」，所以恢复轮单独开 run_id 并留下关联信息
        extra_metadata={
            "resumed": True,
            "resumed_from": payload.resumed_from,
            "approved": payload.approved,
            "interrupt_id": resumed_interrupt_id,
            "approver_user_id": principal.user_id,
        },
    )


@app.get("/audit/runs/{run_id}")
async def get_audit_run(
    run_id: str,
    http_request: Request,
    principal: Principal = Depends(authenticate),
):
    """Return one tenant-owned run and its redacted audit events."""
    if "chat:read" not in principal.scopes:
        raise HTTPException(status_code=403, detail="缺少 chat:read 权限")
    audit = getattr(http_request.app.state, "audit", None)
    if audit is None:
        raise HTTPException(status_code=503, detail="运行审计服务尚未初始化")
    try:
        run = await audit.get_run(principal.tenant_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        events = await audit.list_events(principal.tenant_id, run_id)
        return {"run": run, "events": events}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("读取运行审计失败 run_id=%s", run_id)
        raise HTTPException(status_code=503, detail="运行审计服务暂时不可用") from exc


@app.post("/admin/oidc/revoke")
async def revoke_oidc_token(
    payload: RevokeTokenRequest,
    http_request: Request,
    principal: Principal = Depends(authenticate),
):
    if "security:admin" not in principal.scopes:
        raise HTTPException(status_code=403, detail="缺少 security:admin 权限")
    store = getattr(http_request.app.state, "revocation_store", None)
    if http_request.app.state.settings.auth_mode != "oidc" or store is None:
        raise HTTPException(status_code=503, detail="OIDC 撤销服务尚未启用")
    ttl_seconds = payload.expires_at - int(time.time())
    if ttl_seconds < 1:
        raise HTTPException(status_code=422, detail="expires_at 必须晚于当前时间")
    try:
        await store.revoke(payload.jti, ttl_seconds)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="OIDC 撤销服务暂时不可用") from exc
    http_request.app.state.metrics.increment("oidc_token_revocations_total")
    return {"status": "revoked", "jti": payload.jti}


@app.get("/health")
async def health_check(request: Request):
    """Backward-compatible liveness endpoint; it does not probe dependencies."""
    return {"status": "ok", "agent_ready": request.app.state.agent is not None}


@app.get("/livez")
async def liveness_check():
    return {"status": "ok"}


@app.get("/readyz")
async def readiness_check(request: Request):
    result = await probe_dependencies(request)
    status_code = 200 if result.ok else 503
    return Response(
        content=json.dumps(
            {"status": "ready" if result.ok else "not_ready", "checks": result.checks}
        ),
        media_type="application/json",
        status_code=status_code,
    )


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    settings = request.app.state.settings
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics disabled")
    expected = settings.metrics_auth_token
    if expected and request.headers.get("X-Metrics-Token") != expected:
        raise HTTPException(status_code=401, detail="metrics authentication required")
    payload, content_type = request.app.state.metrics.prometheus_payload()
    # 追加跨进程 Worker 指标（worker 独立进程写 DB，API 聚合输出）。
    worker_payload = await _worker_metrics_payload(request)
    return Response(content=payload + worker_payload, media_type=content_type.split(";", 1)[0])


async def _worker_metrics_payload(request: Request) -> bytes:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not hasattr(runtime, "tickets"):
        return b""
    try:
        rows = await WorkerMetricsDB.snapshot_metrics(runtime.tickets.pool)
    except Exception:
        return b""
    text = prometheus_text(rows)
    if not text:
        return b""
    return f"\n# worker metrics (from worker_metrics table)\n{text}".encode()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        loop="backend.uvicorn_loop:selector_event_loop_factory",
    )
