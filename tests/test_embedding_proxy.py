"""embedding 契约适配代理单元测试 —— 转换逻辑与契约行为。

不依赖真实网络：上游用 httpx.MockTransport 模拟；验证
- transform_upstream：数量/维度/排序/缺字段
- POST / 契约：{"texts"} -> {"embeddings"}，数量与维度强校验
- 上游 4xx/5xx -> 502；错误信息不携带 api_key
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.embedding_proxy import create_app, transform_upstream


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
