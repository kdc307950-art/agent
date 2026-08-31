"""OpenAI 兼容 embedding 契约适配代理 —— 把任意 /v1/embeddings 上游转为项目契约。

背景：项目嵌入契约（backend/knowledge/pgvector.py 的 HttpEmbeddingProvider）是

    POST <KNOWLEDGE_EMBEDDING_ENDPOINT>
    {"texts": ["文本1", ...]}            ->   {"embeddings": [[0.1, ...], ...]}

而 OpenAI 兼容服务的原生 /v1/embeddings 响应是
{"data": [{"embedding": [...], "index": 0, ...}, ...]}——**不兼容**：
HttpEmbeddingProvider 会对 data 里的 dict 做 len()（键数），维度校验必失败。
本代理把任意 OpenAI 兼容上游（OpenAI / 智谱 / 硅基流动 / 本地 vLLM 等）转换为
项目契约，作为 KNOWLEDGE_EMBEDDING_ENDPOINT 指向的端点。

用法（本地连通性自检）：
    uv run python -m backend.embedding_proxy \
        --upstream https://api.openai.com/v1/embeddings \
        --api-key $OPENAI_API_KEY \
        --model text-embedding-3-small \
        --dimension 1536 \
        --port 8100
    curl -X POST http://127.0.0.1:8100/ -H "Content-Type: application/json" \
        -d '{"texts":["你好","世界"]}'

安全：
- api_key 只在转发时放入 Authorization 头，绝不写入日志/错误信息/响应体
- 上游响应体不落日志
"""

from __future__ import annotations

import argparse
import hmac
import logging
import math
import os
import sys
from collections.abc import Sequence
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from uvicorn import Config, Server

logger = logging.getLogger("langgraph.embedding_proxy")

MAX_TEXT_CHARS = 8_192
MAX_REQUEST_BYTES = 1_048_576

# Windows 控制台默认 GBK 编码，强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class EmbeddingRequest(BaseModel):
    """项目契约请求体：texts 为待嵌入文本列表。"""

    texts: list[str] = Field(min_length=1, max_length=32)

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, value: list[str]) -> list[str]:
        if any(not text.strip() for text in value):
            raise ValueError("texts 不能包含空文本")
        if any(len(text) > MAX_TEXT_CHARS for text in value):
            raise ValueError(f"单条文本长度不能超过 {MAX_TEXT_CHARS} 个字符")
        return value


def transform_upstream(
    data: Sequence[dict[str, Any]], *, expected_count: int, dimension: int | None = None
) -> list[list[float]]:
    """把 OpenAI 兼容 data 数组转换为项目契约的 embeddings 数组。

    参数：
        data: 上游响应的 data 列表（每项含 embedding 与 index）
        expected_count: 期望的向量数量（= 请求 texts 数，不符即失败）
        dimension: 期望维度（配置时强校验；None 表示不校验维度）
    返回：按 index 排序的二维浮点数组
    异常：数量不符 / 缺 embedding / 维度不符时抛 ValueError
    """
    if len(data) != expected_count:
        raise ValueError(f"上游返回数量 {len(data)} != 请求数量 {expected_count}")
    indices: list[int] = []
    for item in data:
        try:
            indices.append(int(item["index"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("上游响应项缺少有效 index") from exc
    if sorted(indices) != list(range(expected_count)):
        raise ValueError("上游响应 index 不连续或重复")
    ordered = sorted(data, key=lambda item: int(item["index"]))
    vectors: list[list[float]] = []
    for item in ordered:
        raw = item.get("embedding")
        if not isinstance(raw, (list, tuple)):
            raise ValueError("上游响应项缺少 embedding 数组")
        try:
            vector = [float(value) for value in raw]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("上游响应包含非数字 embedding") from exc
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("上游响应包含非有限 embedding")
        if dimension is not None and len(vector) != dimension:
            raise ValueError(f"embedding 维度 {len(vector)} != 配置 {dimension}")
        vectors.append(vector)
    return vectors


def create_app(
    upstream: str,
    api_key: str | None,
    model: str,
    *,
    dimension: int | None = None,
    proxy_token: str | None = None,
    timeout_seconds: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """构造契约适配代理应用（transport 仅供单元测试注入 MockTransport）。"""
    if not upstream.startswith(("http://", "https://")):
        raise ValueError("upstream 必须是 http(s) URL")
    if dimension is not None and dimension < 1:
        raise ValueError("dimension 必须为正数")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正数")

    app = FastAPI(title="embedding contract proxy", docs_url=None, redoc_url=None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    @app.middleware("http")
    async def enforce_body_limit(request: Request, call_next):
        # Content-Length is a cheap early check; reading the body also covers
        # chunked requests before FastAPI validates the Pydantic payload.
        content_length = request.headers.get("content-length")
        try:
            declared_length = int(content_length) if content_length else 0
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid content length"})
        if declared_length > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"detail": "request body too large"})
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"detail": "request body too large"})
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """平台探活端点：只报告进程存活，不调用付费上游。"""
        return {"status": "ok"}

    @app.post("/")
    async def embed(
        request: EmbeddingRequest,
        x_embedding_proxy_token: str | None = Header(
            default=None, alias="X-Embedding-Proxy-Token"
        ),
    ) -> dict[str, Any]:
        if proxy_token is not None and not (
            x_embedding_proxy_token is not None
            and hmac.compare_digest(x_embedding_proxy_token, proxy_token)
        ):
            raise HTTPException(status_code=401, detail="embedding proxy authentication required")
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, transport=transport
            ) as client:
                response = await client.post(
                    upstream,
                    json={"input": request.texts, "model": model},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            # 不携带 api_key / 上游地址细节，避免凭据或内网信息外泄
            logger.warning("embedding upstream error: %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="embedding upstream unavailable") from exc
        if response.status_code != 200:
            logger.warning("embedding upstream http %s", response.status_code)
            raise HTTPException(status_code=502, detail="embedding upstream error")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("上游响应必须是 JSON 对象")
            data = payload.get("data")
            if not isinstance(data, list):
                raise ValueError("上游响应缺少 data 数组")
            vectors = transform_upstream(
                data, expected_count=len(request.texts), dimension=dimension
            )
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("embedding contract mismatch: %s", exc)
            raise HTTPException(status_code=502, detail="embedding contract mismatch") from exc
        return {"embeddings": vectors}

    return app


def _resolve_config(args: Any) -> dict[str, Any]:
    """解析代理运行配置：CLI 参数优先，其次环境变量（部署平台友好）。

    环境变量回退链：
        upstream  <- EMBEDDING_UPSTREAM
        api_key   <- OPENAI_API_KEY
        model     <- EMBEDDING_MODEL
        dimension <- EMBEDDING_DIMENSION
        host      <- EMBEDDING_HOST
        port      <- EMBEDDING_PORT / PORT（Render 等平台惯例）
    """
    upstream = args.upstream or os.getenv("EMBEDDING_UPSTREAM", "").strip()
    model = args.model or os.getenv("EMBEDDING_MODEL", "").strip()
    api_key = args.api_key or os.getenv("OPENAI_API_KEY", "").strip() or None
    proxy_token = getattr(args, "proxy_token", None) or os.getenv("EMBEDDING_PROXY_TOKEN", "").strip() or None
    host = args.host or os.getenv("EMBEDDING_HOST", "").strip() or "127.0.0.1"
    dimension = args.dimension
    if dimension is None and os.getenv("EMBEDDING_DIMENSION", "").strip():
        try:
            dimension = int(os.getenv("EMBEDDING_DIMENSION", "").strip())
        except ValueError as exc:
            raise SystemExit("EMBEDDING_DIMENSION 必须是整数") from exc
    port = args.port
    if port is None:
        raw_port = os.getenv("EMBEDDING_PORT", "").strip() or os.getenv("PORT", "").strip() or "8100"
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise SystemExit("EMBEDDING_PORT/PORT 必须是整数") from exc
    if not upstream:
        raise SystemExit("缺少 --upstream 或 EMBEDDING_UPSTREAM")
    if not model:
        raise SystemExit("缺少 --model 或 EMBEDDING_MODEL")
    if host not in {"127.0.0.1", "localhost", "::1"} and not proxy_token:
        raise SystemExit("公网监听必须配置 --proxy-token 或 EMBEDDING_PROXY_TOKEN")
    return {
        "upstream": upstream,
        "api_key": api_key,
        "proxy_token": proxy_token,
        "model": model,
        "dimension": dimension,
        "host": host,
        "port": port,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenAI 兼容 embedding 契约适配代理（POST / 接收 {\"texts\": [...]}）"
    )
    parser.add_argument("--upstream", default=None, help="OpenAI 兼容 /v1/embeddings 地址（默认读 EMBEDDING_UPSTREAM）")
    parser.add_argument(
        "--api-key", default=None, help="上游 API Key（默认读 OPENAI_API_KEY 环境变量）"
    )
    parser.add_argument(
        "--proxy-token",
        default=None,
        help="调用方访问令牌（默认读 EMBEDDING_PROXY_TOKEN；公网监听时必填）",
    )
    parser.add_argument("--model", default=None, help="embedding 模型名（默认读 EMBEDDING_MODEL）")
    parser.add_argument(
        "--dimension",
        type=int,
        default=None,
        help="期望维度（强校验；默认读 EMBEDDING_DIMENSION）",
    )
    parser.add_argument("--host", default=None, help="监听地址（默认读 EMBEDDING_HOST，否则 127.0.0.1）")
    parser.add_argument(
        "--port", type=int, default=None, help="监听端口（默认读 EMBEDDING_PORT/PORT，否则 8100）"
    )
    args = parser.parse_args()

    config = _resolve_config(args)
    if not config["api_key"]:
        logger.warning("未提供 --api-key 且 OPENAI_API_KEY 为空：将发送无认证请求")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app(
        config["upstream"],
        config["api_key"],
        config["model"],
        dimension=config["dimension"],
        proxy_token=config["proxy_token"],
    )
    logger.info(
        "embedding proxy 监听 http://%s:%s/ -> %s (model=%s, dimension=%s)",
        config["host"],
        config["port"],
        config["upstream"],
        config["model"],
        config["dimension"],
    )
    Server(Config(app, host=config["host"], port=config["port"])).run()


if __name__ == "__main__":
    main()
