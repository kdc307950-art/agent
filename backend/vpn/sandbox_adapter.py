"""LANGGraph VPN Diagnosis Agent — 可重复沙箱适配器 + 韧性(resilience)包装。

模块归属：backend/vpn。目标（对齐 docs/product/vpn-diagnosis-agent-contract.md §5 与
用户阶段四需求「契约兼容 VPN 沙箱适配器，不接真实生产网关」）：

    - SandboxVpnAdapter：契约兼容 VpnAdapter 的「可重复沙箱数据源」。数据可复用/可重置
      （seed 确定性生成，或由 data/data_path 提供固定快照；reset() 恢复初始快照）。
      默认覆盖 MockVpnAdapter 的全部 8 个契约方法（6 只读 + get_client_config_version +
      受控写 reissue_config），并为「只有三个真实只读能力」的接入点提供确定性数据：
      账号状态 / 客户端配置版本 / 网关健康状态。
    - VpnResilientAdapter：一个「韧性包装基类」，包裹任意 VpnAdapter（Mock/沙箱/未来
      真实适配器），统一注入 request_id / timeout / retry policy / circuit breaker /
      外部错误映射 / 调用审计 / 幂等键 / tenant_id 隔离 / user_id·asset_id 归属校验 /
      脱敏日志。真适配器实现同样 VpnAdapter 契约即可被它包裹。
    - build_vpn_adapter(mode)：数据源优先级工厂。沙箱适配器至少支持前两级：
      mode="mock"（固定 Mock）与 mode="sandbox"（可重复沙箱）。

红线（与 models.FORBIDDEN_COMMANDS 一致，绝不在此实现）：
    重启网关 restart_gateway / 修改防火墙 modify_firewall / 改服务器配置 modify_vpn_config /
    解锁账号 unlock_account / 自动开通权限 grant_vpn_permission 等副作用操作**不提供任何方法**。
    本模块只提供只读查询与「配置交付」受控写 reissue_config（且仅由审批式执行链路触发）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .mock_adapter import MockVpnAdapter, VpnAdapter

logger = logging.getLogger("langgraph.vpn")

# ===========================================================================
# 数据源级别（阶段四优先级：固定Mock → 可重复沙箱 → 测试环境真实API → 生产只读 → 生产受审批写）
# ===========================================================================


class VpnDataSourceTier(StrEnum):
    """VPN 数据源级别枚举（沙箱适配器至少支持前两级：固定 Mock 与可重复沙箱）。"""

    FIXED_MOCK = "mock"            # 固定 Mock（内置/文件快照）
    REPRODUCIBLE_SANDBOX = "sandbox"  # 可重复沙箱（seed 确定性）
    TEST_ENV_REAL = "test_env"     # 测试环境真实 API（本期不接）
    PROD_READONLY = "prod_readonly"   # 生产只读（本期不接）
    PROD_APPROVED_WRITE = "prod_approved_write"  # 生产受审批写（本期不接）


# ===========================================================================
# 脱敏工具（日志/审计不落敏感主键原文）
# ===========================================================================


def redact(value: Any) -> str:
    """把 user_id / asset_id 等敏感主键脱敏为短表示。保留首字符与尾字符，中间打码。"""
    if value is None:
        return "?"
    text = str(value)
    if len(text) <= 3:
        return text[0] + "*" * (len(text) - 1)
    if len(text) == 4:
        return text[:1] + "*" * 2 + text[-1:]
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


_SENSITIVE_KEYS = ("user_id", "asset_id", "incident_id", "requester_id", "owner_user_id")


def desensitize(payload: Any) -> Any:
    """对调用参数/返回做脱敏：敏感键值打码，且不把敏感值原文泄露进任何字符串。

    只处理已知敏感键与出现在 content/文本里的敏感主键，不做整体 dump/深拷贝业务字段。
    """
    # 收集敏感的原始值（用于替换嵌套字符串中的引用）
    sensitive_values: set[str] = set()

    def _collect(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in _SENSITIVE_KEYS and v is not None:
                    sensitive_values.add(str(v))
                _collect(v)
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    _collect(payload)

    def _scrub_text(text: str) -> str:
        for raw in sensitive_values:
            if raw and raw in text:
                text = text.replace(raw, redact(raw))
        return text

    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                if k in _SENSITIVE_KEYS:
                    out[k] = redact(v) if v is not None else v
                elif isinstance(v, str):
                    out[k] = _scrub_text(v)
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(value, list):
            return [_walk(item) for item in value]
        if isinstance(value, str):
            return _scrub_text(value)
        return value

    result = _walk(payload)
    return result


# ===========================================================================
# 重试策略 / 熔断器配置
# ===========================================================================


@dataclass(frozen=True)
class RetryPolicy:
    """指数退避重试策略：max_attempts 次，base_delay 起，指数放大封顶 max_delay。"""

    max_attempts: int = 3
    base_delay: float = 0.05
    max_delay: float = 1.0
    retryable_error_codes: frozenset[str] = frozenset(
        {"timeout", "transient", "connection_reset", "rate_limited"}
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("退避延迟必须 >= 0")

    def delay_for(self, attempt: int) -> float:
        """第 attempt（从 1 计）次失败后的退避延迟，指数放大并封顶。"""
        backoff = self.base_delay * (2 ** (attempt - 1))
        return min(backoff, self.max_delay)


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """熔断器：连续 failure_threshold 次失败即 OPEN；recovery_seconds 后进入 HALF_OPEN。"""

    failure_threshold: int = 5
    recovery_seconds: float = 5.0
    half_open_max_calls: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold 必须 >= 1")
        if self.recovery_seconds < 0:
            raise ValueError("recovery_seconds 必须 >= 0")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls 必须 >= 1")


@dataclass
class CircuitBreaker:
    """状态机熔断器：CLOSED → OPEN → HALF_OPEN →（成功）CLOSED。

    - allow()：是否放行本次调用。
    - record_success()：成功 → 归零失败计数，关闭。
    - record_failure()：连续失败计数达到阈值 → 打开，记录打开时刻。
    - 打开后超过 recovery_seconds 自动进入 HALF_OPEN（限量探测）。
    """

    config: CircuitBreakerConfig

    _failures: int = 0
    _open_since: float = 0.0
    _half_open_calls: int = 0
    _state: str = "closed"  # closed | open | half_open

    def _now(self) -> float:
        return time.monotonic()

    @property
    def state(self) -> str:
        return self._state

    def allow(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if self._now() - self._open_since >= self.config.recovery_seconds:
                # 恢复窗口已到 → 进入半开，允许限量探测
                self._state = "half_open"
                self._half_open_calls = 0
                return True
            return False
        # half_open：限量探测
        return self._half_open_calls < self.config.half_open_max_calls

    def record_success(self) -> None:
        self._failures = 0
        self._half_open_calls = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._half_open_calls += 1
        if self._state != "open":
            self._failures += 1
        if self._failures >= self.config.failure_threshold:
            if self._state != "open":
                self._state = "open"
                self._open_since = self._now()


# ===========================================================================
# 外部错误映射（把外部异常映射为结构化错误 dict）
# ===========================================================================


def default_error_mapper(exc: Exception) -> dict[str, Any]:
    """把外部异常映射为 {found, error_code, reason, retryable, content} 结构。

    缺省映射：TimeoutError→timeout（可重试）；ConnectionError/OSError→transient（可重试）；
    其它→unexpected（不可重试）。子类可 override _map 提供外部错误码映射。
    """
    if isinstance(exc, asyncio.TimeoutError):
        return {
            "found": False,
            "error_code": "timeout",
            "reason": "调用 VPN 数据源超时",
            "retryable": True,
            "content": "VPN 数据源调用超时",
            "error_type": type(exc).__name__,
        }
    if isinstance(exc, ConnectionError):
        return {
            "found": False,
            "error_code": "transient",
            "reason": "VPN 数据源连接异常",
            "retryable": True,
            "content": "VPN 数据源连接异常",
            "error_type": type(exc).__name__,
        }
    return {
        "found": False,
        "error_code": "unexpected",
        "reason": f"VPN 数据源调用异常: {type(exc).__name__}",
        "retryable": False,
        "content": "VPN 数据源调用失败",
        "error_type": type(exc).__name__,
    }


# ===========================================================================
# 访问策略（tenant_id 隔离 / user_id·asset_id 归属校验）
# ===========================================================================


class AccessPolicy:
    """访问策略抽象：authorize 决定 user/asset 是否在 tenant 范围内被允许。

    缺省 allow_all：不做额外校验（供无租户维度的纯 Mock 场景）。
    """

    def authorize(
        self, *, tenant_id: str, user_id: str | None = None, asset_id: str | None = None
    ) -> tuple[bool, str | None]:
        """返回 (ok, reason)；ok=False 时 reason 为拒绝原因。"""
        return True, None


class TenantScopePolicy(AccessPolicy):
    """基于账号/资产目录的 tenant 范围校验。

    - user_id 必须存在于 tenant 的 accounts 目录；
    - asset_id 必须存在且归属（owner_user_id）匹配 user_id（若同时给出）；
    - 当两者都给出时校验归属匹配；只给 user_id 时校验用户存在；只给 asset_id 时校验资产存在。
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        user_ids: set[str] | None = None,
        asset_owners: dict[str, str] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._user_ids = user_ids or set()
        self._asset_owners = asset_owners or {}

    def authorize(
        self, *, tenant_id: str, user_id: str | None = None, asset_id: str | None = None
    ) -> tuple[bool, str | None]:
        if tenant_id != self._tenant_id:
            return False, f"tenant 不匹配: {tenant_id} != {self._tenant_id}"
        if user_id is not None and user_id not in self._user_ids:
            return False, f"用户 {redact(user_id)} 不在租户 {self._tenant_id} 范围"
        if asset_id is not None:
            if asset_id not in self._asset_owners:
                return False, f"资产 {redact(asset_id)} 不在租户 {self._tenant_id} 范围"
            owner = self._asset_owners[asset_id]
            if user_id is not None and owner != user_id:
                return False, f"资产 {redact(asset_id)} 归属 {redact(owner)} 与用户 {redact(user_id)} 不匹配"
        return True, None


ALLOW_ALL_ACCESS: AccessPolicy = AccessPolicy()


# ===========================================================================
# 调用审计
# ===========================================================================


@dataclass
class CallAudit:
    """调用审计记录（内存版；生产可替换为写入 audit.record_event）。"""

    entries: list[dict[str, Any]] = field(default_factory=list)
    max_entries: int = 500

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries :]

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass
class ResilienceConfig:
    """韧性包装的统一配置。"""

    timeout_seconds: float = 3.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    error_mapper: Callable[[Exception], dict[str, Any]] = default_error_mapper

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须 > 0")


# ===========================================================================
# 韧性包装基类：包裹任意 VpnAdapter，注入横切关注点
# ===========================================================================


class VpnResilientAdapter(VpnAdapter):
    """包裹任意 VpnAdapter 的韧性基类，横切注入：request_id / timeout / retry /
    circuit breaker / error mapping / 调用审计 / 幂等键 / tenant 隔离 / 归属校验 / 脱敏日志。

    用法：VpnResilientAdapter(inner=MockVpnAdapter(...), tenant_id="tenant-a", ...)。
    对外暴露与 inner 相同的 VpnAdapter 契约（所有方法委托给 inner），并把每个调用
    包进 _invoke（统一封装韧性关注点）。真实适配器同样只需实现 VpnAdapter 契约即可被包裹。
    """

    # 类级类型声明：供 build_vpn_adapter 工厂动态赋值（data_source_tier/inner_adapter），
    # 保证 mypy 能识别这两个属性而不报 "has no attribute"。实例值在 __init__ 里初始化。
    inner_adapter: VpnAdapter
    data_source_tier: VpnDataSourceTier | None

    def __init__(
        self,
        inner: VpnAdapter,
        *,
        tenant_id: str | None = None,
        access_policy: AccessPolicy = ALLOW_ALL_ACCESS,
        resilience: ResilienceConfig | None = None,
        audit: CallAudit | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._inner = inner
        self._tenant_id = tenant_id
        self._access = access_policy
        self._config = resilience or ResilienceConfig()
        self._audit_log = audit or CallAudit()
        self._breaker = CircuitBreaker(self._config.circuit_breaker)
        # 幂等键登记（按 (method, key) 去重，防重复副作用）
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._new_request_id = request_id_factory or (lambda: uuid.uuid4().hex)
        # 供工厂/外部注入的横向属性（纯类型声明，与 _inner 同源；data_source_tier 由工厂按模式赋值）
        self.inner_adapter: VpnAdapter = inner
        self.data_source_tier: VpnDataSourceTier | None = None

    # ---- 只读查询（全部经 _invoke，具备韧性） ----
    # 先只接三个真实只读能力：账号状态 / 客户端配置版本 / 网关健康状态。
    # 其它方法（asset/incident/similar_tickets/knowledge）契约保留，走同一韧性通道。

    async def get_account_status(self, user_id: str) -> dict[str, Any]:
        _ = user_id
        return await self._invoke("get_account_status", user_id=user_id)

    async def get_client_config_version(self, user_id: str) -> dict[str, Any]:
        return await self._invoke("get_client_config_version", user_id=user_id)

    async def get_gateway_status(
        self, gateway_id: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        return await self._invoke(
            "get_gateway_status", gateway_id=gateway_id, region=region
        )

    async def get_asset(
        self, asset_id: str | None = None, query: str = ""
    ) -> dict[str, Any]:
        return await self._invoke("get_asset", asset_id=asset_id, query=query)

    async def get_incident_status(self, incident_id: str) -> dict[str, Any]:
        return await self._invoke("get_incident_status", incident_id=incident_id)

    async def get_similar_tickets(
        self, user_id: str, fault: str | None = None
    ) -> dict[str, Any]:
        return await self._invoke("get_similar_tickets", user_id=user_id, fault=fault)

    async def search_knowledge(self, query: str, limit: int = 5) -> dict[str, Any]:
        return await self._invoke("search_knowledge", query=query, limit=limit)

    # ---- 受控写：reissue_config（必须带幂等键，且仅由审批式执行链路触发） ----

    async def reissue_config(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        target_version: str | None = None,
    ) -> dict[str, Any]:
        _ = user_id
        return await self._invoke(
            "reissue_config",
            user_id=user_id,
            idempotency_key=idempotency_key,
            target_version=target_version,
            _side_effect=True,
        )

    # ---- 内部：统一韧性通道 ----

    async def _invoke(
        self,
        method: str,
        *,
        _side_effect: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_id = self._new_request_id()
        tenant_id = self._tenant_id or ""
        user_id = kwargs.get("user_id")
        asset_id = kwargs.get("asset_id")

        # 1) tenant_id 隔离 + 归属校验
        ok, reason = self._access.authorize(
            tenant_id=tenant_id, user_id=user_id, asset_id=asset_id
        )
        if not ok:
            deny: dict[str, Any] = {
                "found": False,
                "error_code": "access_denied",
                "reason": reason,
                "request_id": request_id,
                "tenant_id": tenant_id,
            }
            self._record_audit(method, request_id, "denied", deny, elapsed=0.0, tenant_id=tenant_id)
            logger.warning("vpn access denied method=%s tenant=%s reason=%s", method, tenant_id, reason)
            return deny

        # 2) 熔断器门禁
        if not self._breaker.allow():
            blocked: dict[str, Any] = {
                "found": False,
                "error_code": "circuit_open",
                "reason": "VPN 数据源熔断已打开，拒绝调用",
                "request_id": request_id,
                "tenant_id": tenant_id,
            }
            self._record_audit(method, request_id, "circuit_open", blocked, elapsed=0.0, tenant_id=tenant_id)
            return blocked

        # 3) 幂等键（仅副作用操作）：同键已执行过直接返回既有结果
        if _side_effect:
            idem_key = str(kwargs.get("idempotency_key") or "")
            if idem_key:
                cache_key = f"{method}:{idem_key}"
                if cache_key in self._idempotency:
                    return dict(self._idempotency[cache_key])

        # 4) 调 inner（带 timeout + retry）
        retry_policy = self._config.retry_policy
        attempts = retry_policy.max_attempts
        last_error: dict[str, Any] | None = None
        start = time.monotonic()
        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.wait_for(
                    self._dispatch(method, **kwargs),
                    timeout=self._config.timeout_seconds,
                )
                self._breaker.record_success()
                elapsed = time.monotonic() - start
                self._record_audit(method, request_id, "ok", result, elapsed=elapsed, tenant_id=tenant_id)
                if _side_effect and kwargs.get("idempotency_key"):
                    self._idempotency[f"{method}:{kwargs['idempotency_key']}"] = dict(result)
                return result if isinstance(result, dict) else {"found": False, "content": str(result)}
            except Exception as exc:  # noqa: BLE001  外部/内部异常统一映射（含 TimeoutError/ConnectionError）
                mapped = self._config.error_mapper(exc)
                self._breaker.record_failure()
                elapsed = time.monotonic() - start
                self._record_audit(method, request_id, "error", mapped, elapsed=elapsed, tenant_id=tenant_id)
                last_error = mapped
                retryable = bool(mapped.get("retryable")) and bool(
                    mapped.get("error_code") in retry_policy.retryable_error_codes
                )
                if retryable and attempt < attempts:
                    await asyncio.sleep(retry_policy.delay_for(attempt))
                    continue
                break

        result = last_error or {"found": False, "error_code": "unexpected", "reason": "未知错误"}
        result.setdefault("request_id", request_id)
        result.setdefault("tenant_id", tenant_id)
        return result

    def _dispatch(self, method: str, **kwargs: Any) -> Awaitable[Any]:
        """把方法名分派到 inner。inner 若确实实现则直调；否则返回缺省 found:false。"""
        fn = getattr(self._inner, method, None)
        if fn is None:
            async def _missing() -> dict[str, Any]:
                return {"found": False, "content": f"数据源未实现 {method}"}
            return _missing()
        return fn(**kwargs)

    def _record_audit(
        self,
        method: str,
        request_id: str,
        outcome: str,
        payload: dict[str, Any],
        *,
        elapsed: float,
        tenant_id: str,
    ) -> None:
        self._audit_log.record(
            {
                "method": method,
                "request_id": request_id,
                "outcome": outcome,
                "tenant_id": tenant_id,
                "status": payload.get("found", False),
                "error_code": payload.get("error_code"),
                "elapsed_ms": round(elapsed * 1000, 3),
                "args": desensitize(payload),
            }
        )

    # ---- 只读工具需要的内部数据（经 __getattr__ 委托给 inner，保持兼容） ----

    @property
    def audit(self) -> CallAudit:
        return self._audit_log

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._breaker

    def reset_idempotency(self) -> None:
        self._idempotency.clear()

    # 委托 inner 的属性（如 _data / _reissued / _accounts 等），供现有测试与预检复用
    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        return getattr(inner, name)


# ===========================================================================
# 可重复沙箱数据源：seed 确定性生成 + 可重置
# ===========================================================================


def _deterministic_dataset(seed: int) -> dict[str, Any]:
    """用 seed 确定性生成一份沙箱数据集（8 个契约键），保证同 seed 结果可复现。"""
    rng = random.Random(seed)

    accounts: dict[str, Any] = {}
    gateways: dict[str, Any] = {}
    assets: dict[str, Any] = {}
    client_configs: dict[str, Any] = {}
    similar_tickets: dict[str, Any] = {}
    knowledge: list[dict[str, Any]] = [
        {
            "document_id": "vpn-001",
            "title": "VPN 连接失败排查",
            "content": "检查客户端版本、网关负载、账号与证书有效性；确认网络可达与内网路由。",
        },
        {
            "document_id": "vpn-002",
            "title": "VPN 多用户影响升级指引",
            "content": "同一区域多个用户同时故障时应升级为事件并转人工队列，不自动回复。",
        },
    ]
    incidents: dict[str, Any] = {}

    # 确定性生成一组账号 / 资产 / 网关 / 客户端配置
    for i in range(1, 4):
        uid = f"user-sbx-{i:03d}"
        accounts[uid] = {
            "user_id": uid,
            "status": "active",
            "role": "member",
            "expires_at": "2026-12-31",
            "managed_device": True,
            "found": True,
        }
        client_configs[uid] = {
            "user_id": uid,
            "version": f"v2.{seed % 10}.{i}",
            "generated_at": "2026-01-15T00:00:00Z",
            "found": True,
        }
        asset_id = f"asset-sbx-{i:03d}"
        assets[asset_id] = {
            "asset_id": asset_id,
            "hostname": f"laptop-sbx-{i}",
            "name": f"laptop-sbx-{i}",
            "asset_type": "laptop",
            "status": "active",
            "owner_user_id": uid,
            "department": "engineering",
            "found": True,
        }
        similar_tickets.setdefault(uid, [])
        similar_tickets[uid].append(
            {
                "ticket_id": f"T-{100 + i}",
                "status": "resolved",
                "category": "it.vpn",
                "fault": "connection_failed",
                "title": "VPN 频繁掉线",
                "resolved_at": "2025-05-01T10:00:00Z",
            }
        )
        # 网关按租户确定性生成 2 个
        gw_id = f"gw-sbx-{i}"
        gateways[gw_id] = {
            "gateway_id": gw_id,
            "region": "north" if i % 2 else "east",
            "status": "up" if i != 2 else "degraded",
            "load_percent": rng.randint(10, 95),
            "found": True,
        }

    incidents["INC-SBX"] = {
        "incident_id": "INC-SBX",
        "status": "monitoring",
        "severity": "major",
        "affected_user_count": 3,
        "found": True,
    }

    return {
        "accounts": accounts,
        "gateways": gateways,
        "assets": assets,
        "incidents": incidents,
        "similar_tickets": similar_tickets,
        "knowledge": knowledge,
        "client_configs": client_configs,
    }


class SandboxVpnAdapter(VpnAdapter):
    """契约兼容 VpnAdapter 的可重复沙箱数据源（数据可复用/可重置）。

    初始化（优先级：data_path > data > seed 确定性生成 > 内置默认）：
        - seed        : int，确定性生成数据集；同 seed 生成结果一致（可复现）。
        - data        : dict，固定快照（优先于 seed）。
        - data_path   : str，JSON 文件路径固定快照（优先于 data）。
    提供：
        - state_hash()：当前数据快照指纹（用于断言可复现/重置）。
        - reset()     ：恢复初始快照（撤销 reissue_config 类的受控写副作用）。
    契约：实现 VpnAdapter 全部 8 个方法；无任何 restart_gateway / modify_* /
    unlock_account / grant_* 等红线方法。
    """

    def __init__(
        self,
        *,
        seed: int = 0,
        data: dict[str, Any] | None = None,
        data_path: str | None = None,
    ) -> None:
        # 复用 MockVpnAdapter 的 JSON 加载与内置默认（保证底层数据形状一致）
        if data_path or data is not None:
            self._base = MockVpnAdapter(data=data, data_path=data_path)._data
        else:
            # 确定性生成（seed=0 等价默认种子）
            self._base = _deterministic_dataset(seed)
        self._seed = seed
        self._data = json.loads(json.dumps(self._base, ensure_ascii=False))
        self._reissued: dict[str, dict[str, dict[str, Any]]] = {}

    # ---- 可复用/可重置 ----

    def state_hash(self) -> str:
        """当前数据 + 受控写登记的状态指纹（用于断言可复现/重置）。"""
        payload = json.dumps(
            {"data": self._data, "reissued": self._reissued},
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def reset(self) -> None:
        """恢复初始快照（撤销受控写副作用），保证沙箱可重复。"""
        self._data = json.loads(json.dumps(self._base, ensure_ascii=False))
        self._reissued = {}

    # ---- 只读契约方法（确定性数据源实现，无副作用） ----

    async def get_account_status(self, user_id: str) -> dict[str, Any]:
        return await MockVpnAdapter(data=self._data).get_account_status(user_id)

    async def get_client_config_version(self, user_id: str) -> dict[str, Any]:
        return await MockVpnAdapter(data=self._data).get_client_config_version(user_id)

    async def get_gateway_status(
        self, gateway_id: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        return await MockVpnAdapter(data=self._data).get_gateway_status(
            gateway_id=gateway_id, region=region
        )

    async def get_asset(
        self, asset_id: str | None = None, query: str = ""
    ) -> dict[str, Any]:
        return await MockVpnAdapter(data=self._data).get_asset(asset_id=asset_id, query=query)

    async def get_incident_status(self, incident_id: str) -> dict[str, Any]:
        return await MockVpnAdapter(data=self._data).get_incident_status(incident_id)

    async def get_similar_tickets(
        self, user_id: str, fault: str | None = None
    ) -> dict[str, Any]:
        return await MockVpnAdapter(data=self._data).get_similar_tickets(user_id, fault=fault)

    async def search_knowledge(self, query: str, limit: int = 5) -> dict[str, Any]:
        return await MockVpnAdapter(data=self._data).search_knowledge(query, limit=limit)

    # ---- 受控写（审批式执行链路触发；不提供任何红线方法） ----

    async def reissue_config(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        target_version: str | None = None,
    ) -> dict[str, Any]:
        """受控写：重新下发客户端配置。带幂等键；同键重复返回既有结果。"""
        if not idempotency_key:
            return {
                "found": False,
                "delivered": False,
                "confirmed": False,
                "error_code": "missing_idempotency_key",
                "reason": "缺少幂等键，拒绝执行",
                "user_id": user_id,
            }
        # 委托给 MockVpnAdapter（复用其幂等 + 版本更新逻辑），并保持沙箱自身登记同步
        inner = MockVpnAdapter(data=self._data)
        result = await inner.reissue_config(
            user_id=user_id, idempotency_key=idempotency_key, target_version=target_version
        )
        self._data = inner._data
        self._reissued = inner._reissued
        return result

    # ---- 只读内部目录（供访问策略构建与测试断言） ----

    @property
    def accounts(self) -> dict[str, Any]:
        return self._data.get("accounts") or {}

    @property
    def assets(self) -> dict[str, Any]:
        return self._data.get("assets") or {}


# ===========================================================================
# 数据源优先级工厂：至少支持 固定Mock → 可重复沙箱
# ===========================================================================


def build_vpn_adapter(
    mode: str = "sandbox",
    *,
    seed: int = 0,
    data_path: str | None = None,
    tenant_id: str | None = None,
    resilience: ResilienceConfig | None = None,
) -> VpnResilientAdapter:
    """按数据源级别构造「韧性包装的 VPN 适配器」。

    目前支持：
        - mode="mock"    → 固定 Mock（内置/文件快照），tier=FIXED_MOCK
        - mode="sandbox" → 可重复沙箱（seed 确定性），tier=REPRODUCIBLE_SANDBOX
    （test_env / prod_readonly / prod_approved_write 为后续阶段保留，本期不接。）
    """
    mode = mode.lower()
    if mode not in {VpnDataSourceTier.FIXED_MOCK.value, VpnDataSourceTier.REPRODUCIBLE_SANDBOX.value}:
        raise ValueError(
            f"不支持的 VPN 数据源模式: {mode}（当前仅支持 mock / sandbox）"
        )

    if mode == VpnDataSourceTier.FIXED_MOCK.value:
        inner: VpnAdapter = MockVpnAdapter(
            data_path=data_path if data_path else None
        )
    else:
        inner = SandboxVpnAdapter(seed=seed, data_path=data_path)

    resilient = VpnResilientAdapter(
        inner,
        tenant_id=tenant_id,
        resilience=resilience,
    )
    # 让包装暴露数据源级别，便于外层（runtime/tools）识别
    resilient.data_source_tier = VpnDataSourceTier(mode)
    resilient.inner_adapter = inner
    return resilient
