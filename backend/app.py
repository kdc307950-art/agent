from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from langchain_core.messages import HumanMessage

from .security import (
    InMemoryRateLimiter,
    Principal,
    cors_origins,
    rate_limit_dependency,
)
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
                "request_id=%s method=%s path=%s status=%s client=%s elapsed_ms=%.1f",
                request_id,
                request.method,
                request.url.path,
                status_code,
                request.client.host if request.client else "unknown",
                elapsed_ms,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.rate_limiter = InMemoryRateLimiter(
        limit=int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")),
        window_seconds=60,
    )
    logger.info("正在初始化 LangGraph Agent runtime")
    async with runtime_context(settings) as runtime:
        app.state.runtime = runtime
        app.state.agent = runtime.graph
        logger.info("LangGraph Agent runtime 初始化完成")
        yield
    app.state.agent = None
    app.state.runtime = None


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

    physical_thread_id = tenant_thread_id(principal.tenant_id, principal.user_id, payload.thread_id)
    config = {"configurable": {"thread_id": physical_thread_id, "checkpoint_ns": ""}}
    inputs = {"messages": [HumanMessage(content=payload.message)]}

    async def event_generator():
        try:
            timeout_seconds = http_request.app.state.settings.agent_run_timeout_seconds
            async with asyncio.timeout(timeout_seconds):
                async for event in graph.astream(
                    inputs,
                    config=config,
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
                yield "data: {\"type\": \"end\"}\n\n"
        except asyncio.TimeoutError:
            logger.warning("Agent stream timed out request_id=%s", http_request.state.request_id)
            yield "data: {\"type\": \"error\", \"code\": \"agent_timeout\", \"content\": \"请求超时，请稍后重试\"}\n\n"
        except asyncio.CancelledError:
            logger.info("Agent stream cancelled request_id=%s", http_request.state.request_id)
            raise
        except Exception:
            logger.exception("Agent stream failed request_id=%s", http_request.state.request_id)
            yield "data: {\"type\": \"error\", \"code\": \"agent_failed\", \"content\": \"服务暂时不可用，请稍后重试\"}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Request-ID": http_request.state.request_id,
        },
    )


@app.get("/health")
async def health_check(request: Request):
    return {"status": "ok", "agent_ready": request.app.state.agent is not None}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        loop="backend.uvicorn_loop:selector_event_loop_factory",
    )
