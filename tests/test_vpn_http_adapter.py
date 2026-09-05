"""HttpReadonlyVpnAdapter 真实只读 HTTP 数据源适配器单元测试（backend/vpn/http_adapter.py）。

覆盖（阶段五）：
    - 6 个只读契约方法经 HTTP GET 解析回 {found, content, ...} 契约形状；
    - 4xx/5xx → 返回 {found:False, error_code, reason}（不抛异常）；
    - 超时 → asyncio.TimeoutError；连接错误 → ConnectionError（交由外层韧性映射）；
    - 请求头：租户头 + external_request_id 请求头 + 鉴权头；结果回填 external_request_id；
    - 写红线：不提供任何写方法；reissue_config 抛 NotImplementedError；
    - gated 真实 API 测试（需 VPN_HTTP_BASE_URL / VPN_TENANT_ID，缺省 skip）。

使用 httpx.MockTransport 模拟厂商响应，无需真实后端。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

import httpx
import pytest

from backend.vpn.http_adapter import (
    HttpReadonlyVpnAdapter,
    HttpVpnConfig,
    build_http_config_from_env,
)
from backend.vpn.sandbox_adapter import ResilienceConfig, RetryPolicy, VpnResilientAdapter

Handler = Callable[[httpx.Request], httpx.Response]


def _config() -> HttpVpnConfig:
    return HttpVpnConfig(base_url="https://vpn.example.com", api_key="key-1", tenant_id="tenant-a")


def _adapter(handler: Handler, *, captured: list[httpx.Request] | None = None) -> HttpReadonlyVpnAdapter:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return HttpReadonlyVpnAdapter(_config(), client=client)


def _run(coro):
    return asyncio.run(coro)


# 一个覆盖 6 个端点 + 错误 + 超时的通用 handler
def _default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/accounts/user-x":
        return httpx.Response(200, json={"found": True, "status": "active", "role": "member",
                                         "expires_at": "2026-12-31", "user_id": "user-x"})
    if path == "/accounts/user-x/client-config":
        return httpx.Response(200, json={"found": True, "version": "v2.4.1", "generated_at": "2026-01-15T00:00:00Z",
                                         "user_id": "user-x"})
    if path == "/gateways/gw-1":
        return httpx.Response(200, json={"found": True, "gateway_id": "gw-1", "region": "north",
                                         "status": "up", "load_percent": 42})
    if path == "/assets/a-1":
        return httpx.Response(200, json={"found": True, "asset_id": "a-1", "hostname": "h1",
                                         "asset_type": "laptop", "status": "active",
                                         "owner_user_id": "user-x"})
    if path == "/incidents/INC-9":
        return httpx.Response(200, json={"found": True, "incident_id": "INC-9", "status": "monitoring",
                                         "severity": "major", "affected_user_count": 25})
    if path == "/users/user-x/similar-tickets":
        return httpx.Response(200, json={"found": True, "tickets": [
            {"ticket_id": "T-1", "status": "resolved", "category": "it.vpn", "fault": "connection_failed",
             "title": "VPN 频繁掉线", "resolved_at": "2025-05-01T10:00:00Z"}
        ]})
    if path == "/accounts/ghost":
        return httpx.Response(200, json={"found": False, "content": "未找到"})
    if path == "/gateways/gw-missing":
        return httpx.Response(404, json={})
    if path == "/incidents/bad":
        return httpx.Response(500, json={})
    if path == "/timeout":
        raise httpx.ReadTimeout("read timeout")
    if path == "/conn":
        raise httpx.ConnectError("connect refused")
    return httpx.Response(200, json={"found": False})


# ===========================================================================
# 6 个只读契约方法：解析回契约形状
# ===========================================================================


def test_get_account_status_parsed():
    result = _run(_adapter(_default_handler).get_account_status("user-x"))
    assert result.get("found") is True
    assert result.get("status") == "active"
    assert "content" in result


def test_get_client_config_version_parsed():
    result = _run(_adapter(_default_handler).get_client_config_version("user-x"))
    assert result.get("found") is True
    assert result.get("version") == "v2.4.1"


def test_get_gateway_status_parsed():
    result = _run(_adapter(_default_handler).get_gateway_status(gateway_id="gw-1"))
    assert result.get("found") is True
    assert result.get("status") == "up"


def test_get_asset_parsed():
    result = _run(_adapter(_default_handler).get_asset(asset_id="a-1"))
    assert result.get("found") is True
    assert result.get("asset_id") == "a-1"


def test_get_incident_status_parsed():
    result = _run(_adapter(_default_handler).get_incident_status("INC-9"))
    assert result.get("found") is True
    assert result.get("severity") == "major"


def test_get_similar_tickets_parsed():
    result = _run(_adapter(_default_handler).get_similar_tickets("user-x", fault="connection_failed"))
    assert result.get("found") is True
    assert isinstance(result.get("tickets"), list)
    assert len(result.get("tickets")) == 1
    assert result.get("tickets")[0]["ticket_id"] == "T-1"


def test_not_found_returns_found_false():
    result = _run(_adapter(_default_handler).get_account_status("ghost"))
    assert result.get("found") is False


# ===========================================================================
# 错误 / 超时 / 连接错误
# ===========================================================================


def test_http_error_returns_structured_error_not_raise():
    result = _run(_adapter(_default_handler).get_gateway_status(gateway_id="gw-missing"))
    assert result.get("found") is False
    assert result.get("error_code") == "http_error"
    assert result.get("reason")  # 有原因说明


def test_http_5xx_returns_structured_error():
    result = _run(_adapter(_default_handler).get_incident_status("bad"))
    assert result.get("found") is False
    assert result.get("error_code") == "http_error"


def _timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("read timeout")


def _connect_error_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def test_timeout_raises_asyncio_timeout():
    """直接调用适配器：httpx 读超时被转换为 asyncio.TimeoutError（交由外层韧性映射）。"""
    with pytest.raises(asyncio.TimeoutError):
        _run(_adapter(_timeout_handler).get_account_status("user-x"))


def test_timeout_mapped_by_wrapper():
    """超时经 VpnResilientAdapter 统一映射为 timeout（可重试），不抛异常给调用方。"""
    resilient = VpnResilientAdapter(
        _adapter(_timeout_handler),
        resilience=ResilienceConfig(
            timeout_seconds=2.0,
            retry_policy=RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0),
        ),
    )
    result = _run(resilient.get_account_status("user-x"))
    assert result.get("found") is False
    assert result.get("error_code") == "timeout"
    assert result.get("retryable") is True


def test_connection_error_raises_connection_error():
    with pytest.raises(ConnectionError):
        _run(_adapter(_connect_error_handler).get_account_status("user-x"))


# ===========================================================================
# 请求头：租户 / external_request_id / 鉴权
# ===========================================================================


def test_headers_sent_to_vendor():
    captured: list[httpx.Request] = []
    adapter = _adapter(_default_handler, captured=captured)
    result = _run(adapter.get_account_status("user-x", external_request_id="EXT-HDR"))
    assert len(captured) == 1
    req = captured[0]
    assert req.headers.get("X-Tenant-Id") == "tenant-a"
    assert req.headers.get("X-Request-Id") == "EXT-HDR"
    assert req.headers.get("Authorization") == "Bearer key-1"
    assert result.get("external_request_id") == "EXT-HDR"


def test_external_request_id_auto_generated_when_not_given():
    result = _run(_adapter(_default_handler).get_account_status("user-x"))
    assert result.get("external_request_id") is not None


# ===========================================================================
# 写红线：不提供任何写方法
# ===========================================================================


def test_http_adapter_has_no_write_methods():
    adapter = _adapter(_default_handler)
    for name in (
        "reissue_config",
        "restart_gateway",
        "modify_firewall",
        "modify_vpn_config",
        "unlock_account",
        "grant_vpn_permission",
        "toggle_gateway",
    ):
        # 约束：除了继承自基类 VpnAdapter 的 reissue_config（其调用即抛 NotImplementedError），
        # 其余写方法必须完全缺失。
        if name == "reissue_config":
            with pytest.raises(NotImplementedError):
                _run(getattr(adapter, name)(user_id="x", idempotency_key="k"))
        else:
            assert not hasattr(adapter, name), f"红线方法 {name} 不应存在"


# ===========================================================================
# build_http_config_from_env（env 分层配置）
# ===========================================================================


def test_build_http_config_from_env(monkeypatch):
    monkeypatch.setenv("VPN_HTTP_BASE_URL", "https://vpn.example.com")
    monkeypatch.setenv("VPN_TENANT_ID", "tenant-a")
    monkeypatch.setenv("VPN_API_KEY", "k")
    monkeypatch.setenv("VPN_HTTP_CONNECT_TIMEOUT", "2.5")
    monkeypatch.setenv("VPN_HTTP_READ_TIMEOUT", "6")
    cfg = build_http_config_from_env()
    assert cfg.base_url == "https://vpn.example.com"
    assert cfg.tenant_id == "tenant-a"
    assert cfg.connect_timeout == 2.5
    assert cfg.read_timeout == 6


def test_build_http_config_from_env_missing_raises(monkeypatch):
    monkeypatch.delenv("VPN_HTTP_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="VPN_HTTP_BASE_URL"):
        build_http_config_from_env()


# ===========================================================================
# gated 真实 API 测试（需 VPN_HTTP_BASE_URL / VPN_TENANT_ID / VPN_API_KEY）
# ===========================================================================

REAL_BASE = os.getenv("VPN_HTTP_BASE_URL", "").strip()
REAL_TENANT = os.getenv("VPN_TENANT_ID", "").strip()


@pytest.mark.skipif(
    not (REAL_BASE and REAL_TENANT),
    reason="需要设置 VPN_HTTP_BASE_URL / VPN_TENANT_ID（真实只读 VPN API）",
)
@pytest.mark.skip(reason="真实 API 连通性测试：由 CI/具备凭据的环境执行")
def test_real_readonly_api_account_status():
    """真实只读 API 命中：契约查询返回 found:true。

    通过环境变量 VPN_HTTP_BASE_URL / VPN_TENANT_ID / VPN_API_KEY 定位真实只读端点；
    其它契约方法（get_client_config_version / get_gateway_status / get_asset /
    get_incident_status / get_similar_tickets）在此环境也按相同方式验证（此处取账号状态作代表）。
    """
    cfg = build_http_config_from_env()
    adapter = HttpReadonlyVpnAdapter(cfg)
    result = asyncio.run(adapter.get_account_status("user-042"))
    assert result.get("found") is True
    assert result.get("status") == "active"
