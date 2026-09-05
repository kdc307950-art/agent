"""Unit tests for the explicit FortiManager JSON-RPC write gateway."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.vpn.fortimanager_gateway import (
    FortiManagerCommandGateway,
    FortiManagerConfig,
    FortiManagerConfigurationError,
    FortiManagerTarget,
    targets_from_json,
)


def _gateway(handler, *, max_wait: float = 0.05) -> FortiManagerCommandGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FortiManagerCommandGateway(
        FortiManagerConfig(
            base_url="https://fmg.example.com",
            api_token="token-1",
            poll_interval_seconds=0.001,
            max_wait_seconds=max_wait,
        ),
        {
            "tenant-a": FortiManagerTarget(
                adom="ADOM_A", device="FGT_A", vdom="root", package="PKG_A"
            )
        },
        client=client,
    )


def test_reissue_submits_package_then_confirms_task_without_user_path_inference():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.headers["Authorization"] == "Bearer token-1"
        url = payload["params"][0]["url"]
        if url == "/securityconsole/install/package":
            data = payload["params"][0]["data"]
            assert data == {
                "adom": "ADOM_A",
                "flags": ["none"],
                "pkg": "PKG_A",
                "scope": [{"name": "FGT_A", "vdom": "root"}],
            }
            return httpx.Response(
                200, json={"result": [{"status": {"code": 0}, "data": {"task": 91}}]}
            )
        assert url == "/task/task/91"
        return httpx.Response(
            200,
            json={
                "result": [
                    {"status": {"code": 0}, "data": {"percent": 100, "num_err": 0, "state": "done"}}
                ]
            },
        )

    gateway = _gateway(handler)
    result = asyncio.run(
        gateway.reissue_config(
            tenant_id="tenant-a",
            user_id="user-42",
            idempotency_key="idem-42",
            target_version="v2.5",
        )
    )
    assert result["confirmed"] is True
    assert result["vendor_task_id"] == 91
    assert len(calls) == 2
    asyncio.run(gateway.aclose())


def test_running_task_returns_timeout_with_task_id_for_reconciliation():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        url = payload["params"][0]["url"]
        if url == "/securityconsole/install/package":
            return httpx.Response(
                200, json={"result": [{"status": {"code": 0}, "data": {"task": 92}}]}
            )
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "status": {"code": 0},
                        "data": {"percent": 20, "num_err": 0, "state": "running"},
                    }
                ]
            },
        )

    gateway = _gateway(handler, max_wait=0.003)
    result = asyncio.run(
        gateway.reissue_config(tenant_id="tenant-a", user_id="u", idempotency_key="idem-92")
    )
    assert result["error_code"] == "vendor_timeout"
    assert result["vendor_task_id"] == 92
    asyncio.run(gateway.aclose())


def test_task_failure_exposes_vendor_details_without_claiming_delivery():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        url = payload["params"][0]["url"]
        if url == "/securityconsole/install/package":
            return httpx.Response(
                200, json={"result": [{"status": {"code": 0}, "data": {"task": 93}}]}
            )
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "status": {"code": 0},
                        "data": {
                            "percent": 100,
                            "num_err": 1,
                            "state": "done",
                            "line": [{"detail": "install failed"}],
                        },
                    }
                ]
            },
        )

    gateway = _gateway(handler)
    result = asyncio.run(
        gateway.reissue_config(tenant_id="tenant-a", user_id="u", idempotency_key="idem-93")
    )
    assert result["delivered"] is False
    assert result["error_code"] == "fortimanager_task_failed"
    assert "install failed" in result["reason"]
    asyncio.run(gateway.aclose())


def test_unknown_tenant_is_rejected_before_any_network_write():
    gateway = _gateway(lambda request: pytest.fail("不应发出网络请求"))
    with pytest.raises(FortiManagerConfigurationError, match="未配置"):
        asyncio.run(gateway.submit_reissue_config(tenant_id="tenant-b", idempotency_key="idem"))
    asyncio.run(gateway.aclose())


def test_target_json_requires_explicit_package_mapping():
    targets = targets_from_json(
        '{"tenant-a": {"adom": "ADOM_A", "device": "FGT_A", "vdom": "root", "package": "PKG_A"}}'
    )
    assert targets["tenant-a"].package == "PKG_A"
    with pytest.raises(FortiManagerConfigurationError, match="package"):
        targets_from_json('{"tenant-a": {"adom": "ADOM_A", "device": "FGT_A"}}')
