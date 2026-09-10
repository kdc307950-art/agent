"""VpnResilientAdapter 阶段五韧性扩展单元测试（backend/vpn/sandbox_adapter.py）。

覆盖（阶段五）：
    - 只读结果缓存：同请求窗口内不重复打厂商（调用计数 == 1）；缓存命中返回副本；
      禁用缓存 / TTL 过期 → 重新打厂商；副作用 reissue_config 永不进结果缓存。
    - external_request_id + trace_id：每次读调用都落进「返回结果」与「审计条目」。
    - 写红线（生产第一阶段只读）：real HttpReadonlyVpnAdapter 不提供任何写方法；
      经 VpnResilientAdapter 包装后调用 reissue_config 也零副作用。
    - build_vpn_adapter 环境分层：mock / sandbox / real 三级；real 未注入 config 且缺
      环境变量时抛 ValueError；未支持模式（prod_readonly 等）继续抛错（向后兼容）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.vpn.http_adapter import HttpReadonlyVpnAdapter, HttpVpnConfig
from backend.vpn.mock_adapter import VpnAdapter
from backend.vpn.sandbox_adapter import (
    CallAudit,
    ResilienceConfig,
    ResultCache,
    VpnDataSourceTier,
    VpnResilientAdapter,
    build_vpn_adapter,
)


def _run(coro):
    return asyncio.run(coro)


class _CountingAdapter(VpnAdapter):
    """计数 + 记录 external_request_id 的假只读适配器。"""

    _accepts_external_request_id = True

    def __init__(self):
        self.calls = 0
        self.reissue_calls = 0
        self.seen_external_ids: list[str | None] = []

    async def get_account_status(self, user_id, *, external_request_id=None):
        self.calls += 1
        self.seen_external_ids.append(external_request_id)
        return {"found": True, "status": "active", "user_id": user_id, "content": "ok"}

    async def get_gateway_status(self, gateway_id=None, region=None, **kw):
        return {"found": False}

    async def get_asset(self, asset_id=None, query="", **kw):
        return {"found": False}

    async def get_incident_status(self, incident_id, **kw):
        return {"found": False}

    async def get_similar_tickets(self, user_id, fault=None, **kw):
        return {"found": False}

    async def search_knowledge(self, query, limit=5, **kw):
        return {"found": False}

    async def get_client_config_version(self, user_id, **kw):
        return {"found": False}

    async def reissue_config(self, *, user_id, idempotency_key, target_version=None):
        self.reissue_calls += 1
        return {
            "found": True,
            "delivered": True,
            "user_id": user_id,
            "idempotency_key": idempotency_key,
        }


# ===========================================================================
# 只读结果缓存
# ===========================================================================


def test_result_cache_hits_within_ttl():
    """同请求在 TTL 窗口内只打一次厂商（第二次走到缓存命中）。"""
    inner = _CountingAdapter()
    audit = CallAudit()
    resilient = VpnResilientAdapter(
        inner,
        resilience=ResilienceConfig(cache_ttl_seconds=5.0),
        audit=audit,
    )
    r1 = _run(resilient.get_account_status("user-x"))
    r2 = _run(resilient.get_account_status("user-x"))
    assert inner.calls == 1, "TTL 窗口内第二次应命中缓存，不再打厂商"
    assert r1.get("found") is True and r2.get("found") is True
    assert r1.get("status") == r2.get("status")
    # 二次调用返回的是缓存副本（可安全被调用方修改而不污染缓存）
    r2["status"] = "hacked"
    r3 = _run(resilient.get_account_status("user-x"))
    assert r3.get("status") == "active", "缓存返回副本，调用方修改不应污染缓存"
    # 审计里应出现 cache_hit 记录
    outcomes = [e["outcome"] for e in audit.entries]
    assert "cache_hit" in outcomes
    assert inner.calls == 1


def test_result_cache_disabled_by_default():
    """默认配置不缓存：连续调用每次都打厂商。"""
    inner = _CountingAdapter()
    resilient = VpnResilientAdapter(inner)  # 默认 ResilienceConfig 无缓存 TTL
    _run(resilient.get_account_status("user-x"))
    _run(resilient.get_account_status("user-x"))
    assert inner.calls == 2


def test_cache_hit_reinjects_current_trace_id():
    """缓存命中时回填「本次调用」的新 trace_id，保证逐调用可审计。"""
    inner = _CountingAdapter()
    cache = ResultCache(ttl_seconds=5.0)
    resilient = VpnResilientAdapter(
        inner, cache=cache, resilience=ResilienceConfig(cache_ttl_seconds=5.0)
    )
    r1 = _run(resilient.get_account_status("user-x"))
    r2 = _run(resilient.get_account_status("user-x"))
    assert r1.get("trace_id") != r2.get("trace_id"), "缓存命中应回填本次调用的新 trace_id"
    assert inner.calls == 1


def test_result_cache_expires_after_ttl():
    """TTL 过期后强制重新打厂商。"""
    inner = _CountingAdapter()
    cache = ResultCache(ttl_seconds=5.0)
    resilient = VpnResilientAdapter(
        inner, cache=cache, resilience=ResilienceConfig(cache_ttl_seconds=5.0)
    )
    _run(resilient.get_account_status("user-x"))
    assert inner.calls == 1
    # 手动使缓存条目过期
    now = time.monotonic()
    for k, (_, val) in list(cache._store.items()):
        cache._store[k] = (now - 100, val)
    _run(resilient.get_account_status("user-x"))
    assert inner.calls == 2, "TTL 过期后应重新打厂商"


def test_side_effect_not_result_cached():
    """副作用 reissue_config 不进结果缓存：不同幂等键会真正再次执行。"""
    inner = _CountingAdapter()
    resilient = VpnResilientAdapter(inner, resilience=ResilienceConfig(cache_ttl_seconds=5.0))
    _run(resilient.reissue_config(user_id="user-x", idempotency_key="k1"))
    _run(resilient.reissue_config(user_id="user-x", idempotency_key="k2"))
    assert inner.reissue_calls == 2, "副作用必须真正执行（不经结果缓存）"


def test_side_effect_same_key_idempotent():
    """同幂等键副作用幂等：第二次不重复执行（走幂等键登记，二者独立于结果缓存）。"""
    inner = _CountingAdapter()
    resilient = VpnResilientAdapter(inner, resilience=ResilienceConfig(cache_ttl_seconds=5.0))
    r1 = _run(resilient.reissue_config(user_id="user-x", idempotency_key="k1"))
    r2 = _run(resilient.reissue_config(user_id="user-x", idempotency_key="k1"))
    assert inner.reissue_calls == 1
    assert r1.get("idempotency_key") == r2.get("idempotency_key")


# ===========================================================================
# external_request_id + trace_id（结果与审计均可审计）
# ===========================================================================


def test_external_request_id_carried_to_inner_and_result():
    """每次读调用的 external_request_id 透传给 inner，且回填进返回结果。"""
    inner = _CountingAdapter()
    resilient = VpnResilientAdapter(inner, external_request_id_factory=lambda: "EXT-42")
    result = _run(resilient.get_account_status("user-x"))
    assert inner.seen_external_ids == ["EXT-42"], "external_request_id 应透传给 inner"
    assert result.get("external_request_id") == "EXT-42"
    assert result.get("trace_id") is not None
    assert result.get("request_id") == result.get("trace_id")


def test_audit_records_trace_id_and_external_request_id():
    """审计条目必须同时携带 trace_id 与 external_request_id（所有读取请求可审计）。"""
    inner = _CountingAdapter()
    audit = CallAudit()
    resilient = VpnResilientAdapter(
        inner,
        audit=audit,
        resilience=ResilienceConfig(cache_ttl_seconds=5.0),
        external_request_id_factory=lambda: "EXT-1",
    )
    _run(resilient.get_account_status("user-x"))
    _run(resilient.get_client_config_version("user-x"))
    _run(resilient.get_gateway_status(gateway_id="gw-1"))
    for entry in audit.entries:
        assert entry.get("trace_id") is not None, "审计需要携带 trace_id"
        assert entry.get("external_request_id") == "EXT-1", "审计需要携带 external_request_id"
        assert entry.get("method") in {
            "get_account_status",
            "get_client_config_version",
            "get_gateway_status",
        }


# ===========================================================================
# 写红线：真实只读适配器不提供任何写方法（生产第一阶段只读）
# ===========================================================================


def _http_adapter() -> HttpReadonlyVpnAdapter:
    return HttpReadonlyVpnAdapter(
        HttpVpnConfig(base_url="https://vpn.example.com", api_key="key", tenant_id="tenant-a")
    )


def test_http_adapter_has_no_forbidden_redline_methods():
    """红线操作（重启网关/改防火墙/改服务器配置/解锁/自动开通/切换网关）绝不提供方法。"""
    adapter = _http_adapter()
    for forbidden in (
        "restart_gateway",
        "modify_firewall",
        "modify_vpn_config",
        "modify_server_config",
        "unlock_account",
        "grant_vpn_permission",
        "toggle_gateway",
    ):
        assert not hasattr(adapter, forbidden), f"红线方法 {forbidden} 不应存在"


def test_http_adapter_reissue_config_not_available():
    """reissue_config 在真实只读适配器上不可用：调用即抛 NotImplementedError。"""
    adapter = _http_adapter()
    with pytest.raises(NotImplementedError):
        _run(adapter.reissue_config(user_id="user-x", idempotency_key="k1"))


def test_real_wrapped_reissue_never_writes():
    """经 VpnResilientAdapter 包装的真实只读适配器，reissue_config 也无法产生副作用。"""
    adapter = _http_adapter()
    resilient = VpnResilientAdapter(adapter)
    result = _run(resilient.reissue_config(user_id="user-x", idempotency_key="k1"))
    assert result.get("found") is False
    assert result.get("error_code") in {"unexpected", "not_implemented"}
    # 真实适配器确实没有实现 reissue_config（无副作用可发生）
    assert not hasattr(adapter, "modify_vpn_config")


# ===========================================================================
# build_vpn_adapter 环境分层：mock / sandbox / real
# ===========================================================================


def test_build_real_mode_injected_config():
    adapter = build_vpn_adapter(
        "real",
        http_config=HttpVpnConfig(base_url="https://vpn.example.com", api_key="k", tenant_id="t"),
    )
    assert adapter.data_source_tier == VpnDataSourceTier.PROD_READONLY
    assert isinstance(adapter.inner_adapter, HttpReadonlyVpnAdapter)
    assert isinstance(adapter, VpnResilientAdapter)


def test_build_real_mode_from_env(monkeypatch):
    monkeypatch.setenv("VPN_HTTP_BASE_URL", "https://vpn.example.com")
    monkeypatch.setenv("VPN_TENANT_ID", "tenant-a")
    monkeypatch.setenv("VPN_API_KEY", "k")
    adapter = build_vpn_adapter("real")
    assert adapter.data_source_tier == VpnDataSourceTier.PROD_READONLY
    assert isinstance(adapter.inner_adapter, HttpReadonlyVpnAdapter)


def test_build_real_mode_missing_env_raises(monkeypatch):
    monkeypatch.delenv("VPN_HTTP_BASE_URL", raising=False)
    monkeypatch.delenv("VPN_TENANT_ID", raising=False)
    with pytest.raises(ValueError, match="VPN_HTTP_BASE_URL"):
        build_vpn_adapter("real")


def test_build_real_mode_still_rejects_unsupported_mode():
    """向后兼容：prod_readonly 等未支持模式继续抛错。"""
    with pytest.raises(ValueError, match="不支持的 VPN 数据源模式"):
        build_vpn_adapter("prod_readonly")


def test_build_mock_and_sandbox_still_work():
    assert build_vpn_adapter("mock").data_source_tier == VpnDataSourceTier.FIXED_MOCK
    assert (
        build_vpn_adapter("sandbox", seed=1).data_source_tier
        == VpnDataSourceTier.REPRODUCIBLE_SANDBOX
    )


def test_custom_result_cache_ttl_via_build():
    """build_vpn_adapter 允许注入 ResultCache 以启用缓存。"""
    inner_target = build_vpn_adapter("mock", cache=ResultCache(ttl_seconds=5.0))
    assert inner_target._cache is not None and inner_target._cache.ttl_seconds == 5.0
