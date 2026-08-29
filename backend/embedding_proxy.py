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
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from uvicorn import Config, Server

logger = logging.getLogger("langgraph.embedding_proxy")

# Windows 控制台默认 GBK 编码，强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class EmbeddingRequest(BaseModel):
    """项目契约请求体：texts 为待嵌入文本列表。"""

    texts: list[str] = Field(min_length=1, max_length=32)


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
    ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
    vectors: list[list[float]] = []
    for item in ordered:
        raw = item.get("embedding")
        if not isinstance(raw, (list, tuple)):
            raise ValueError("上游响应项缺少 embedding 数组")
        vector = [float(value) for value in raw]
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
    timeout_seconds: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """构造契约适配代理应用（transport 仅供单元测试注入 MockTransport）。"""
    if not upstream.startswith(("http://", "https://")):
        raise ValueError("upstream 必须是 http(s) URL")

    app = FastAPI(title="embedding contract proxy", docs_url=None, redoc_url=None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    @app.post("/")
    async def embed(request: EmbeddingRequest) -> dict[str, Any]:
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
            data = payload.get("data")
            if not isinstance(data, list):
                raise ValueError("上游响应缺少 data 数组")
            vectors = transform_upstream(
                data, expected_count=len(request.texts), dimension=dimension
            )
        except ValueError as exc:
            logger.warning("embedding contract mismatch: %s", exc)
            raise HTTPException(status_code=502, detail="embedding contract mismatch") from exc
        return {"embeddings": vectors}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenAI 兼容 embedding 契约适配代理（POST / 接收 {\"texts\": [...]}）"
    )
    parser.add_argument("--upstream", required=True, help="OpenAI 兼容 /v1/embeddings 地址")
    parser.add_argument(
        "--api-key", default=None, help="上游 API Key（默认读 OPENAI_API_KEY 环境变量）"
    )
    parser.add_argument("--model", required=True, help="embedding 模型名（上游透传）")
    parser.add_argument(
        "--dimension",
        type=int,
        default=None,
        help="期望维度（强校验；与 KNOWLEDGE_EMBEDDING_DIMENSION 一致）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8100, help="监听端口（默认 8100）")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("OPENAI_API_KEY", "").strip() or None
    if not api_key:
        logger.warning("未提供 --api-key 且 OPENAI_API_KEY 为空：将发送无认证请求")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app(
        args.upstream, api_key, args.model, dimension=args.dimension
    )
    logger.info(
        "embedding proxy 监听 http://%s:%s/ -> %s (model=%s, dimension=%s)",
        args.host,
        args.port,
        args.upstream,
        args.model,
        args.dimension,
    )
    Server(Config(app, host=args.host, port=args.port)).run()


if __name__ == "__main__":
    main()
