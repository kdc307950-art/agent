"""SandboxVpnAdapter 与 VpnResilientAdapter 集成测试（backend/vpn/sandbox_adapter.py）。

覆盖（阶段四）：
    - 契约兼容：SandboxVpnAdapter 实现 VpnAdapter 全部 8 个方法 + 无红线方法（restart_gateway 等）。
    - 可重复/可重置：同 seed 生成状态指纹一致；不同 seed 不同；reset() 撤销受控写副作用。
    - 三个真实只读能力：账号状态 / 客户端配置版本 / 网关健康状态（确定性命中）。
    - tenant_id 隔离 + user_id/asset_id 归属校验：跨租户 / 不相属 → access_denied。
    - 脱敏：调用审计不落敏感主键原文。
    - 重试/熔断：transient 错误可重试；连续失败触发熔断 open。
    - 幂等键：reissue_config 同键去重，不重复产生副作用。
    - 数据源优先级工厂：build_vpn_adapter 支持 mock / sandbox 两级。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.vpn.mock_adapter import VpnAdapter
from backend.vpn.sandbox_adapter import (
    ALLOW_ALL_ACCESS,
    CallAudit,
    CircuitBreaker,
    CircuitBreakerConfig,
    ResilienceConfig,
    RetryPolicy,
    SandboxVpnAdapter,
    TenantScopePolicy,
    VpnDataSourceTier,
    VpnResilientAdapter,
    build_vpn_adapter,
    desensitize,
    redact,
)


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# 契约兼容：全部 VpnAdapter 方法存在，且无任何红线方法
# ===========================================================================


def test_sandbox_adapter_is_contract_compatible():
    """SandboxVpnAdapter 必须实现 VpnAdapter 契约的全部 8 个公开方法。"""
    adapter = SandboxVpnAdapter(seed=3)
    for method in (
        "get_account_status",
        "get_gateway_status",
        "get_asset",
        "get_incident_status",
        "get_similar_tickets",
        "search_knowledge",
        "get_client_config_version",
        "reissue_config",
    ):
        assert callable(getattr(adapter, method)), f"缺少契约方法 {method}"
    assert isinstance(adapter, VpnAdapter)


def test_sandbox_adapter_has_no_forbidden_redline_methods():
    """红线操作（重启网关/改防火墙/改服务器配置/解锁/自动开通权限）绝不提供方法。"""
    adapter = SandboxVpnAdapter()
    for forbidden in (
        "restart_gateway",
        "modify_firewall",
        "modify_vpn_config",
        "unlock_account",
        "grant_vpn_permission",
        "toggle_gateway",
    ):
        assert not hasattr(adapter, forbidden), f"红线方法 {forbidden} 不应存在"


# ===========================================================================
# 可重复 / 可重置（seed 确定性）
# ===========================================================================


def test_same_seed_produces_same_state_hash():
    a = SandboxVpnAdapter(seed=7)
    b = SandboxVpnAdapter(seed=7)
    assert a.state_hash() == b.state_hash()


def test_different_seed_produces_different_state_hash():
    a = SandboxVpnAdapter(seed=7)
    b = SandboxVpnAdapter(seed=8)
    assert a.state_hash() != b.state_hash()


def test_reset_restores_initial_snapshot():
    """reissue_config 是受控写（改版本）；reset() 应恢复初始快照。"""
    adapter = SandboxVpnAdapter(seed=1)
    before = adapter.state_hash()
    result = _run(
        adapter.reissue_config(
            user_id="user-sbx-001", idempotency_key="k-1", target_version="v9.9.9"
        )
    )
    assert result.get("delivered") is True
    after = adapter.state_hash()
    assert after != before, "受控写应改变数据快照"
    adapter.reset()
    assert adapter.state_hash() == before, "reset() 应恢复初始快照"


def test_reissue_config_requires_idempotency_key():
    adapter = SandboxVpnAdapter(seed=1)
    result = _run(adapter.reissue_config(user_id="user-sbx-001", idempotency_key=""))
    assert result.get("error_code") == "missing_idempotency_key"
    assert result.get("delivered") is False


# ===========================================================================
# 三个真实只读能力：账号状态 / 客户端配置版本 / 网关健康状态
# ===========================================================================


def test_readonly_account_status_hit():
    adapter = SandboxVpnAdapter(seed=0)
    result = _run(adapter.get_account_status("user-sbx-001"))
    assert result.get("found") is True
    assert result.get("status") == "active"


def test_readonly_client_config_version_hit():
    adapter = SandboxVpnAdapter(seed=0)
    result = _run(adapter.get_client_config_version("user-sbx-001"))
    assert result.get("found") is True
    assert result.get("version")


def test_readonly_gateway_status_hit():
    adapter = SandboxVpnAdapter(seed=0)
    result = _run(adapter.get_gateway_status(gateway_id="gw-sbx-1"))
    assert result.get("found") is True
    assert result.get("status") == "up"


# ===========================================================================
# tenant_id 隔离 + user_id/asset_id 归属校验
# ===========================================================================


def _tenant_policy() -> TenantScopePolicy:
    return TenantScopePolicy(
        tenant_id="tenant-a",
        user_ids={"user-sbx-001"},
        asset_owners={"asset-sbx-001": "user-sbx-001"},
    )


def test_cross_tenant_user_denied():
    """跨租户查询：user 不在 tenant-a 范围 → access_denied。"""
    resilient = VpnResilientAdapter(
        SandboxVpnAdapter(seed=0),
        tenant_id="tenant-a",
        access_policy=_tenant_policy(),
    )
    result = _run(resilient.get_account_status("user-sbx-002"))
    assert result.get("error_code") == "access_denied"
    assert result.get("found") is False


def test_cross_tenant_asset_denied():
    resilient = VpnResilientAdapter(
        SandboxVpnAdapter(seed=0),
        tenant_id="tenant-a",
        access_policy=_tenant_policy(),
    )
    result = _run(resilient.get_asset(asset_id="asset-sbx-002"))
    assert result.get("error_code") == "access_denied"


def test_asset_owner_mismatch_denied():
    """归属校验：资产在租户范围内，但归属与查询用户不匹配 → 拒绝。"""
    policy = TenantScopePolicy(
        tenant_id="tenant-a",
        user_ids={"user-sbx-001", "user-sbx-002"},
        asset_owners={"asset-sbx-001": "user-sbx-001"},
    )
    ok, reason = policy.authorize(
        tenant_id="tenant-a", user_id="user-sbx-002", asset_id="asset-sbx-001"
    )
    assert ok is False
    assert "不匹配" in (reason or "")


def test_same_tenant_user_allowed():
    resilient = VpnResilientAdapter(
        SandboxVpnAdapter(seed=0),
        tenant_id="tenant-a",
        access_policy=_tenant_policy(),
    )
    result = _run(resilient.get_account_status("user-sbx-001"))
    assert result.get("found") is True


def test_tenant_isolation_when_no_policy_allow_all():
    """未配置访问策略时默认 ALLOW_ALL：不阻断（兼容纯 Mock 场景）。"""
    resilient = VpnResilientAdapter(
        SandboxVpnAdapter(seed=0), tenant_id=None, access_policy=ALLOW_ALL_ACCESS
    )
    result = _run(resilient.get_account_status("user-sbx-001"))
    assert result.get("found") is True


# ===========================================================================
# 脱敏（日志/审计不落敏感主键原文）
# ===========================================================================


def test_redact_hides_middle_of_identifier():
    assert redact("user-sbx-001") == "us********01"
    assert redact(None) == "?"


def test_desensitize_mask_sensitive_keys():
    raw = {"user_id": "user-sbx-001", "asset_id": "asset-sbx-001", "incident_id": "INC-9"}
    out = desensitize(raw)
    assert "user-sbx-001" not in out["user_id"]
    assert "asset-sbx-001" not in out["asset_id"]
    assert "INC-9" not in out["incident_id"]


def test_audit_does_not_log_raw_sensitive_ids():
    audit = CallAudit()
    resilient = VpnResilientAdapter(
        SandboxVpnAdapter(seed=0),
        tenant_id="tenant-a",
        access_policy=_tenant_policy(),
        audit=audit,
    )
    _run(resilient.get_account_status("user-sbx-001"))
    assert len(audit.entries) == 1
    # 审计里不能出现真实 user_id 原文
    assert "user-sbx-001" not in str(audit.entries[0])


# ===========================================================================
# 重试 / 熔断
# ===========================================================================


class _FlakyAdapter(VpnAdapter):
    """前 fail_times 次抛 ConnectionError，之后成功（用于测试重试）。"""

    def __init__(self, fail_times: int = 2):
        self.fail_times = fail_times
        self.calls = 0

    async def get_account_status(self, user_id: str) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("transient failure")
        return {"found": True, "status": "active", "user_id": user_id, "content": "ok"}

    # 其余契约方法未用到，仅占位。
    async def get_gateway_status(self, gateway_id=None, region=None):
        return {"found": False}
    async def get_asset(self, asset_id=None, query=""):
        return {"found": False}
    async def get_incident_status(self, incident_id):
        return {"found": False}
    async def get_similar_tickets(self, user_id, fault=None):
        return {"found": False}
    async def search_knowledge(self, query, limit=5):
        return {"found": False}
    async def get_client_config_version(self, user_id):
        return {"found": False}
    async def reissue_config(self, *, user_id, idempotency_key, target_version=None):
        return {"found": False}


def test_transient_error_retried_then_success():
    inner = _FlakyAdapter(fail_times=2)
    resilient = VpnResilientAdapter(
        inner,
        resilience=ResilienceConfig(
            timeout_seconds=2.0,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0),
        ),
    )
    result = _run(resilient.get_account_status("user-x"))
    assert result.get("found") is True
    assert inner.calls == 3, "transient 错误应重试到成功"


def test_non_retryable_error_not_retried():
    """unexpected 错误不可重试：只调用 1 次。"""

    class _BrokenAdapter(VpnAdapter):
        def __init__(self):
            self.calls = 0

        async def get_account_status(self, user_id: str) -> dict[str, Any]:
            self.calls += 1
            raise RuntimeError("boom")

        async def get_gateway_status(self, gateway_id=None, region=None):
            return {"found": False}
        async def get_asset(self, asset_id=None, query=""):
            return {"found": False}
        async def get_incident_status(self, incident_id):
            return {"found": False}
        async def get_similar_tickets(self, user_id, fault=None):
            return {"found": False}
        async def search_knowledge(self, query, limit=5):
            return {"found": False}
        async def get_client_config_version(self, user_id):
            return {"found": False}
        async def reissue_config(self, *, user_id, idempotency_key, target_version=None):
            return {"found": False}

    inner = _BrokenAdapter()
    resilient = VpnResilientAdapter(
        inner,
        resilience=ResilienceConfig(
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0)
        ),
    )
    result = _run(resilient.get_account_status("user-x"))
    assert result.get("found") is False
    assert result.get("error_code") == "unexpected"
    assert inner.calls == 1


def test_circuit_breaker_opens_after_failure_threshold():
    inner = _FlakyAdapter(fail_times=99)  # 一直失败
    breaker = CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=3, recovery_seconds=10.0, half_open_max_calls=1)
    )
    resilient = VpnResilientAdapter(
        inner,
        resilience=ResilienceConfig(
            retry_policy=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0),
            circuit_breaker=CircuitBreakerConfig(
                failure_threshold=3, recovery_seconds=10.0, half_open_max_calls=1
            ),
        ),
    )
    # 替换 breaker 为手工实例以控制状态
    resilient._breaker = breaker

    results = [_run(resilient.get_account_status("user-x")) for _ in range(4)]
    # 前 3 次失败累积到阈值 → 第 4 次熔断拒绝
    assert results[3].get("error_code") == "circuit_open"
    assert breaker.state == "open"


def test_breaker_state_transitions_after_recovery():
    breaker = CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=1, recovery_seconds=5.0, half_open_max_calls=1)
    )
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == "open"
    # 恢复窗口内仍 open（拒绝调用）
    assert breaker.allow() is False
    # 模拟恢复窗口已过 → 进入 half_open 允许探测
    breaker._open_since = 0.0
    assert breaker.allow() is True
    assert breaker.state == "half_open"
    breaker.record_success()
    assert breaker.state == "closed"


# ===========================================================================
# 幂等键（reissue_config 同键去重）
# ===========================================================================


def test_reissue_idempotency_key_dedupes():
    inner = SandboxVpnAdapter(seed=1)
    resilient = VpnResilientAdapter(inner)
    r1 = _run(resilient.reissue_config(user_id="user-sbx-001", idempotency_key="idem-1", target_version="v2.5.0"))
    r2 = _run(resilient.reissue_config(user_id="user-sbx-001", idempotency_key="idem-1", target_version="v2.5.0"))
    assert r1.get("delivered") is True and r2.get("delivered") is True
    # 二次调用返回同一幂等键既有结果（不重复副作用）
    assert r1.get("idempotency_key") == r2.get("idempotency_key")


# ===========================================================================
# 数据源优先级工厂：支持 mock / sandbox 两级
# ===========================================================================


def test_build_vpn_adapter_sandbox_tier():
    adapter = build_vpn_adapter("sandbox", seed=5)
    assert adapter.data_source_tier == VpnDataSourceTier.REPRODUCIBLE_SANDBOX
    assert isinstance(adapter, VpnResilientAdapter)
    assert isinstance(adapter.inner_adapter, SandboxVpnAdapter)


def test_build_vpn_adapter_mock_tier():
    from backend.vpn.mock_adapter import MockVpnAdapter

    adapter = build_vpn_adapter("mock")
    assert adapter.data_source_tier == VpnDataSourceTier.FIXED_MOCK
    assert isinstance(adapter.inner_adapter, MockVpnAdapter)


def test_build_vpn_adapter_rejects_unsupported_mode():
    with pytest.raises(ValueError, match="不支持的 VPN 数据源模式"):
        build_vpn_adapter("prod_readonly")


def test_sandbox_supports_three_real_readonly_via_factory():
    """工厂产物：账号状态 / 客户端配置版本 / 网关健康状态三只读能力命中。"""
    adapter = build_vpn_adapter("sandbox", seed=0)
    acct = _run(adapter.get_account_status("user-sbx-001"))
    cfg = _run(adapter.get_client_config_version("user-sbx-001"))
    gw = _run(adapter.get_gateway_status(gateway_id="gw-sbx-1"))
    assert acct.get("found") is True
    assert cfg.get("found") is True
    assert gw.get("found") is True
