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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from langchain_core.messages import HumanMessage

from .security import (
    authenticate,
    InMemoryRateLimiter,
    OIDCVerifier,
    Principal,
    cors_origins,
    rate_limit_dependency,
)
from .rate_limit import RedisRateLimiter
from .revocation import RedisRevocationStore
from .audit import NoopAuditRepository
from .metrics import RuntimeMetrics
from .run_context import RunContext
from .runtime import runtime_context
from .repositories import tenant_thread_id
from .settings import Settings


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


class RevokeTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jti: str = Field(min_length=8, max_length=256, pattern=r"^[A-Za-z0-9._:-]+$")
    expires_at: int = Field(gt=0)


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
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.metrics = RuntimeMetrics()
    app.state.audit = NoopAuditRepository()
    app.state.revocation_store = None
    redis_client = None
    auth_verifier = None
    try:
        if settings.rate_limit_backend == "redis" or (
            settings.auth_mode == "oidc" and settings.oidc_revocation_mode == "redis"
        ):
            redis_client = redis.from_url(
                settings.redis_url,
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
                    raise RuntimeError("Redis 不可用，按 fail-closed 策略拒绝启动")
                redis_client = None

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
            revocation_store = RedisRevocationStore(redis_client) if settings.oidc_revocation_mode == "redis" and redis_client else None
            if settings.oidc_revocation_mode == "redis" and revocation_store is None:
                raise RuntimeError("OIDC_REVOCATION_MODE=redis 但 Redis 不可用")
            app.state.revocation_store = revocation_store
            auth_verifier = OIDCVerifier(
                issuer=settings.oidc_issuer_url,
                audience=settings.oidc_audience,
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
        logger.info("正在初始化 LangGraph Agent runtime auth_mode=%s rate_limit_backend=%s", settings.auth_mode, settings.rate_limit_backend)
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
        if hasattr(app.state, "metrics"):
            app.state.metrics.shutdown()


app = FastAPI(title="LangGraph Agent API", lifespan=lifespan)
app.add_middleware(AuditMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    http_request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """流式对话端点（Server-Sent Events）。"""
    graph = http_request.app.state.agent
    if graph is None:
        raise HTTPException(status_code=503, detail="Agent 尚未初始化")

    run_id = uuid4().hex
    http_request.state.run_id = run_id
    http_request.state.tenant_hash = hashlib.sha256(principal.tenant_id.encode("utf-8")).hexdigest()[:16]
    http_request.app.state.metrics.increment("agent_runs_total")
    physical_thread_id = tenant_thread_id(principal.tenant_id, principal.user_id, payload.thread_id)
    config = {"configurable": {"thread_id": physical_thread_id, "checkpoint_ns": ""}}
    inputs = {"messages": [HumanMessage(content=payload.message)]}
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
    audit = getattr(http_request.app.state, "audit", NoopAuditRepository())
    try:
        async with asyncio.timeout(settings.audit_write_timeout_seconds):
            await audit.start_run(
                run_context,
                metadata={"route": http_request.url.path},
            )
    except Exception as exc:
        http_request.app.state.metrics.increment("audit_errors_total")
        logger.exception("无法创建运行审计记录 request_id=%s run_id=%s", run_context.request_id, run_id)
        raise HTTPException(status_code=503, detail="运行审计服务暂时不可用") from exc

    async def finish_run(status: str, *, error_code: str | None = None) -> None:
        try:
            async with asyncio.timeout(settings.audit_write_timeout_seconds):
                await audit.finish_run(
                    run_context,
                    status,
                    error_code=error_code,
                    metadata={"route": http_request.url.path},
                )
        except Exception:
            http_request.app.state.metrics.increment("audit_errors_total")
            logger.exception("无法更新运行审计状态 request_id=%s run_id=%s status=%s", run_context.request_id, run_id, status)

    async def event_generator():
        try:
            async with asyncio.timeout(settings.agent_run_timeout_seconds):
                async for event in graph.astream(
                    inputs,
                    config=config,
                    context=run_context,
                    stream_mode="updates",
                ):
                    for node_name, update in event.items():
                        if node_name == "agent" and "messages" in update:
                            msg = update["messages"][0]
                            if getattr(msg, "content", None):
                                yield f"data: {json.dumps({'type': 'text', 'content': msg.content}, ensure_ascii=False)}\n\n"
                            if getattr(msg, "tool_calls", None):
                                yield f"data: {json.dumps({'type': 'tool', 'status': 'calling'}, ensure_ascii=False)}\n\n"
                        elif node_name == "tools":
                            yield f"data: {json.dumps({'type': 'tool', 'status': 'done'}, ensure_ascii=False)}\n\n"
                await finish_run("completed")
                http_request.app.state.metrics.increment("agent_runs_completed_total")
                yield f"data: {json.dumps({'type': 'end', 'run_id': run_id}, ensure_ascii=False)}\n\n"
        except asyncio.TimeoutError:
            await finish_run("timeout", error_code="agent_timeout")
            http_request.app.state.metrics.increment("agent_timeouts_total")
            logger.warning("Agent stream timed out request_id=%s run_id=%s", http_request.state.request_id, run_id)
            yield f"data: {json.dumps({'type': 'error', 'code': 'agent_timeout', 'run_id': run_id, 'content': '请求超时，请稍后重试'}, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            await finish_run("cancelled", error_code="client_cancelled")
            http_request.app.state.metrics.increment("agent_cancellations_total")
            logger.info("Agent stream cancelled request_id=%s run_id=%s", http_request.state.request_id, run_id)
            raise
        except Exception:
            await finish_run("failed", error_code="agent_failed")
            http_request.app.state.metrics.increment("agent_errors_total")
            logger.exception("Agent stream failed request_id=%s run_id=%s", http_request.state.request_id, run_id)
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
    return {"status": "ok", "agent_ready": request.app.state.agent is not None}


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    settings = request.app.state.settings
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics disabled")
    expected = settings.metrics_auth_token
    if expected and request.headers.get("X-Metrics-Token") != expected:
        raise HTTPException(status_code=401, detail="metrics authentication required")
    payload, content_type = request.app.state.metrics.prometheus_payload()
    return Response(content=payload, media_type=content_type.split(";", 1)[0])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        loop="backend.uvicorn_loop:selector_event_loop_factory",
    )
