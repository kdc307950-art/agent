"""embedding 契约适配代理单元测试 —— 转换逻辑与契约行为。

不依赖真实网络：上游用 httpx.MockTransport 模拟；验证
- transform_upstream：数量/维度/排序/缺字段
- POST / 契约：{"texts"} -> {"embeddings"}，数量与维度强校验
- 上游 4xx/5xx -> 502；错误信息不携带 api_key
"""

from __future__ import annotations

import json
from argparse import Namespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.embedding_proxy import _resolve_config, create_app, transform_upstream


def test_transform_upstream_sorts_by_index_and_validates_count():
    data = [
        {"index": 1, "embedding": [3.0, 4.0]},
        {"index": 0, "embedding": [1.0, 2.0]},
    ]
    vectors = transform_upstream(data, expected_count=2)
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]


def test_transform_upstream_rejects_count_mismatch():
    with pytest.raises(ValueError, match="数量"):
        transform_upstream([{"index": 0, "embedding": [1.0]}], expected_count=2)


def test_transform_upstream_rejects_dimension_mismatch():
    with pytest.raises(ValueError, match="维度"):
        transform_upstream(
            [{"index": 0, "embedding": [1.0, 2.0]}], expected_count=1, dimension=1536
        )


def test_transform_upstream_rejects_missing_embedding():
    with pytest.raises(ValueError, match="embedding"):
        transform_upstream([{"index": 0}], expected_count=1)


def test_transform_upstream_rejects_duplicate_or_missing_indexes():
    with pytest.raises(ValueError, match="index"):
        transform_upstream(
            [
                {"index": 0, "embedding": [1.0, 2.0]},
                {"index": 0, "embedding": [3.0, 4.0]},
            ],
            expected_count=2,
        )


def _app_with_upstream(
    responses: list[httpx.Response],
) -> tuple[TestClient, list[dict]]:
    """构造注入 MockTransport 的代理；记录上游收到的请求体与头。"""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "url": str(request.url),
                "body": json.loads(request.content.decode("utf-8")),
                "auth": request.headers.get("authorization", ""),
            }
        )
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    app = create_app(
        "https://upstream.example/v1/embeddings",
        "secret-key-123",
        "test-model",
        dimension=2,
        transport=transport,
    )
    return TestClient(app), captured


def test_proxy_translates_contract_and_forwards_auth():
    client, captured = _app_with_upstream(
        [
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [1.0, 2.0]},
                        {"index": 1, "embedding": [3.0, 4.0]},
                    ]
                },
            )
        ]
    )
    response = client.post("/", json={"texts": ["你好", "世界"]})
    assert response.status_code == 200
    assert response.json() == {"embeddings": [[1.0, 2.0], [3.0, 4.0]]}
    # 上游收到 OpenAI 兼容格式与 Authorization 头
    assert captured[0]["url"] == "https://upstream.example/v1/embeddings"
    assert captured[0]["body"] == {"input": ["你好", "世界"], "model": "test-model"}
    assert captured[0]["auth"] == "Bearer secret-key-123"


def test_proxy_rejects_count_mismatch_without_leaking_key():
    client, _ = _app_with_upstream(
        [httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})]
    )
    response = client.post("/", json={"texts": ["a", "b"]})
    assert response.status_code == 502
    body = response.text
    assert "secret-key-123" not in body  # 凭据绝不进入响应


def test_proxy_rejects_upstream_http_error_without_detail():
    client, _ = _app_with_upstream([httpx.Response(401, json={"error": "invalid key"})])
    response = client.post("/", json={"texts": ["a"]})
    assert response.status_code == 502
    assert "invalid key" not in response.text  # 上游错误细节不外泄


def test_proxy_rejects_dimension_mismatch():
    client, _ = _app_with_upstream(
        [httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})]
    )
    response = client.post("/", json={"texts": ["a"]})
    assert response.status_code == 502
    assert "contract mismatch" in response.json()["detail"]


def test_proxy_requires_token_when_configured_and_exposes_healthz():
    app = create_app(
        "https://upstream.example/v1/embeddings",
        "secret-key-123",
        "test-model",
        dimension=2,
        proxy_token="proxy-secret",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]}
            )
        ),
    )
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.post("/", json={"texts": ["a"]}).status_code == 401
    assert (
        client.post(
            "/",
            json={"texts": ["a"]},
            headers={"X-Embedding-Proxy-Token": "proxy-secret"},
        ).status_code
        == 200
    )


def test_proxy_rejects_blank_and_oversized_texts():
    client, _ = _app_with_upstream([])
    assert client.post("/", json={"texts": [" "]}).status_code == 422
    assert client.post("/", json={"texts": ["x" * 8193]}).status_code == 422


def _empty_args() -> Namespace:
    return Namespace(
        upstream=None,
        api_key=None,
        proxy_token=None,
        model=None,
        dimension=None,
        host=None,
        port=None,
    )


def test_resolve_config_prefers_cli_over_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_UPSTREAM", "https://env.example/v1/embeddings")
    monkeypatch.setenv("EMBEDDING_MODEL", "env-model")
    config = _resolve_config(
        Namespace(
            upstream="https://cli.example/v1/embeddings",
            api_key="cli-key",
            proxy_token="proxy-key",
            model="cli-model",
            dimension=1024,
            host="0.0.0.0",
            port=9000,
        )
    )
    assert config["upstream"] == "https://cli.example/v1/embeddings"
    assert config["api_key"] == "cli-key"
    assert config["proxy_token"] == "proxy-key"
    assert config["model"] == "cli-model"
    assert config["dimension"] == 1024
    assert config["host"] == "0.0.0.0"
    assert config["port"] == 9000


def test_resolve_config_reads_env_fallbacks(monkeypatch):
    monkeypatch.setenv("EMBEDDING_UPSTREAM", "https://env.example/v1/embeddings")
    monkeypatch.setenv("EMBEDDING_MODEL", "env-model")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    monkeypatch.setenv("EMBEDDING_HOST", "0.0.0.0")
    monkeypatch.setenv("EMBEDDING_PORT", "8200")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("EMBEDDING_PROXY_TOKEN", "proxy-key")
    config = _resolve_config(_empty_args())
    assert config["upstream"] == "https://env.example/v1/embeddings"
    assert config["model"] == "env-model"
    assert config["dimension"] == 1024
    assert config["host"] == "0.0.0.0"
    assert config["port"] == 8200
    assert config["api_key"] == "env-key"
    assert config["proxy_token"] == "proxy-key"


def test_resolve_config_falls_back_to_render_port(monkeypatch):
    monkeypatch.setenv("EMBEDDING_UPSTREAM", "https://env.example/v1/embeddings")
    monkeypatch.setenv("EMBEDDING_MODEL", "env-model")
    monkeypatch.delenv("EMBEDDING_PORT", raising=False)
    monkeypatch.setenv("PORT", "10000")  # Render 惯例
    config = _resolve_config(_empty_args())
    assert config["port"] == 10000
    assert config["host"] == "127.0.0.1"


def test_resolve_config_requires_upstream_and_model(monkeypatch):
    monkeypatch.delenv("EMBEDDING_UPSTREAM", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    with pytest.raises(SystemExit, match="EMBEDDING_UPSTREAM"):
        _resolve_config(_empty_args())
    monkeypatch.setenv("EMBEDDING_UPSTREAM", "https://env.example/v1/embeddings")
    with pytest.raises(SystemExit, match="EMBEDDING_MODEL"):
        _resolve_config(_empty_args())
