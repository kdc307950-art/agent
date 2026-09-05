"""占位演练：FortiManager 写入网关的 env 装配与未覆盖分支（staging 未定前的本地绿线）。

对应桌面《Fortinet-代码现状审计与决策清单.md》§1.2 覆盖缺口：
  1. build_fortimanager_gateway_from_env() 的 env 装配路径（占位值演练）
  2. install_kind=device 分支（dev_rev_comments 注入 + 无 pkg 键）+ access_user header
  3. FMG 业务拒绝（status.code != 0 → fortimanager_rejected）
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.vpn.fortimanager_gateway import (
    FortiManagerCommandGateway,
    FortiManagerConfig,
    FortiManagerTarget,
    build_fortimanager_gateway_from_env,
)

PLACEHOLDER_TARGETS_JSON = json.dumps(
    {
        "t_demo_uat": {
            "adom": "root",
            "device": "FGT-PLACEHOLDER-001",
            "vdom": "root",
            "package": "PKG_PLACEHOLDER",
            "install_kind": "package",
        }
    }
)


def _set_placeholder_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VPN_FMG_BASE_URL", "https://fmg.placeholder.invalid")
    monkeypatch.setenv("VPN_FMG_API_TOKEN", "placeholder-token")
    monkeypatch.setenv("VPN_FMG_TENANT_TARGETS_JSON", PLACEHOLDER_TARGETS_JSON)
    monkeypatch.setenv("VPN_FMG_VERIFY_TLS", "false")


def test_from_env_builds_gateway_from_placeholder_targets(monkeypatch: pytest.MonkeyPatch):
    """占位 env → from_env 装配成功、targets 按映射解析（不发网络请求）。"""
    _set_placeholder_env(monkeypatch)
    gateway = build_fortimanager_gateway_from_env()
    try:
        # 私有 target 表已验证到 tenant；对未配置租户必须拒绝
        assert "t_demo_uat" in gateway._targets
        target = gateway._targets["t_demo_uat"]
        assert target.adom == "root"
        assert target.device == "FGT-PLACEHOLDER-001"
        assert target.package == "PKG_PLACEHOLDER"
        assert target.install_kind == "package"
        with pytest.raises(Exception, match="未配置"):
            gateway._target("t_unknown")
    finally:
        asyncio.run(gateway.aclose())


def test_device_install_kind_payload_and_access_user_header():
    """install_kind=device：url 为 install/device、无 pkg 键、dev_rev_comments 只含幂等键前缀；
    配置 access_user 时每个请求都带该 header。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.headers.get("access_user") == "api_agent"
        assert request.headers["Authorization"] == "Bearer token-1"
        url = payload["params"][0]["url"]
        if url == "/securityconsole/install/device":
            data = payload["params"][0]["data"]
            assert data["adom"] == "ADOM_B"
            assert data["flags"] == ["none"]
            assert data["scope"] == [{"name": "FGT_B", "vdom": "root"}]
            assert "pkg" not in data
            assert data["dev_rev_comments"].startswith("vpn-agent approved install ")
            assert "very-secret-user-name" not in data["dev_rev_comments"]
            return httpx.Response(
                200, json={"result": [{"status": {"code": 0}, "data": {"task": 7}}]}
            )
        assert url == "/task/task/7"
        return httpx.Response(
            200,
            json={
                "result": [
                    {"status": {"code": 0}, "data": {"percent": 100, "num_err": 0, "state": "done"}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = FortiManagerCommandGateway(
        FortiManagerConfig(
            base_url="https://fmg.example.com",
            api_token="token-1",
            access_user="api_agent",
            poll_interval_seconds=0.001,
            max_wait_seconds=0.05,
        ),
        {
            "tenant-b": FortiManagerTarget(
                adom="ADOM_B", device="FGT_B", vdom="root", install_kind="device"
            )
        },
        client=client,
    )
    result = asyncio.run(
        gateway.reissue_config(
            tenant_id="tenant-b",
            user_id="very-secret-user-name",
            idempotency_key="idem-device-001",
        )
    )
    assert result["confirmed"] is True
    assert result["vendor_task_id"] == 7
    assert len(calls) == 2  # 1 install 提交 + 1 task 查询；task 获得后不二次 install
    asyncio.run(gateway.aclose())


def test_business_rejection_maps_to_fortimanager_rejected():
    """FMG 业务拒绝（HTTP 200 但 status.code != 0）→ fortimanager_rejected，不声称交付。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        url = payload["params"][0]["url"]
        if url == "/securityconsole/install/package":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "status": {"code": -11, "message": "No permission for the resource"},
                            "url": "/securityconsole/install/package",
                        }
                    ]
                },
            )
        raise AssertionError(f"拒绝后不应再查询任务: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = FortiManagerCommandGateway(
        FortiManagerConfig(
            base_url="https://fmg.example.com",
            api_token="token-1",
            poll_interval_seconds=0.001,
            max_wait_seconds=0.05,
        ),
        {
            "tenant-a": FortiManagerTarget(
                adom="ADOM_A", device="FGT_A", vdom="root", package="PKG_A"
            )
        },
        client=client,
    )
    result = asyncio.run(
        gateway.reissue_config(tenant_id="tenant-a", user_id="u", idempotency_key="idem-rej")
    )
    assert result["error_code"] == "fortimanager_rejected"
    assert "No permission" in result["reason"]
    assert result["confirmed"] is False
    assert result["delivered"] is False
    asyncio.run(gateway.aclose())
