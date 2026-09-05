"""LANGGraph VPN Diagnosis Agent — 真实只读 HTTP VPN 数据源适配器（staging/production 只读层）。

模块归属：backend/vpn。目标（对齐 docs/product/vpn-diagnosis-agent-contract.md §5 与
阶段四/五「真实只读数据源 + 环境分层」）：

    - HttpReadonlyVpnAdapter：契约兼容 VpnAdapter 的「真实只读」HTTP 适配器。对任意
      只读 contract 方法，通过 HTTP GET 一个 base_url 端点，携带租户隔离与请求追踪头，
      把厂商 JSON 解析回 VpnAdapter 的统一契约形状（{found, content, ...}）。
    - 本类**只实现 6 个只读契约方法**（get_account_status / get_client_config_version /
      get_gateway_status / get_asset / get_incident_status / get_similar_tickets）。
      它**不实现任何写方法**：reissue_config 沿用基类 VpnAdapter 的 NotImplementedError；
      restart_gateway / modify_* / unlock_account / grant_* 等红线方法在类上完全缺失。
      这是「生产第一阶段只读」的硬红线（自动写 / 破坏性动作在真源上不可能发生）。
    - HttpVpnConfig：连接/读超时、租户、鉴权头、追踪头的统一配置。
    - build_http_config_from_env()：从 VPN_HTTP_* 环境变量构造 HttpVpnConfig（缺必填则抛错，
      供 build_vpn_adapter("real") 在构建期调用；不污染 settings.from_env 的导入期）。

安全/韧性约定：
    - 超时（httpx.TimeoutException → asyncio.TimeoutError）、连接错误（httpx.TransportError
      → ConnectionError）会被抛出，交由外层 VpnResilientAdapter 统一做错误映射 + 重试 + 熔断；
    - 4xx/5xx HTTP 错误**不抛异常**，返回 {found:False, error_code, reason} 结构化错误；
    - 每个请求携带 external_request_id（透传调用方生成），并作为请求头发给厂商；
      成功结果里回填 external_request_id，若响应头带 X-Trace-Id（厂商追踪），一并回填 vendor_trace_id。
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .mock_adapter import VpnAdapter

logger = logging.getLogger("langgraph.vpn")

# 端点模板（相对 base_url）。真实端点不同时，子类 override `_build_url`。
# gateway / asset 支持 by-id 或 by-query，故用函数式拼装而非纯模板。
_ENDPOINTS = {
    "get_account_status": "/accounts/{user_id}",
    "get_client_config_version": "/accounts/{user_id}/client-config",
    "get_gateway_status": "/gateways",
    "get_asset": "/assets",
    "get_incident_status": "/incidents/{incident_id}",
    "get_similar_tickets": "/users/{user_id}/similar-tickets",
}


@dataclass(frozen=True)
class HttpVpnConfig:
    """真实只读 HTTP VPN 数据源的配置（staging/production 层）。

    必填：base_url / tenant_id。/ api_key 可空（有些鉴权走租户/请求头）。
    可选：connect_timeout / read_timeout / max_retries（外层韧性仍会再限制一次，
    但此处的超时作为底层兜底，防止单次 HTTP 挂起过久）/ auth_header / tenant_header /
    request_id_header。
    """

    base_url: str
    api_key: str
    tenant_id: str
    connect_timeout: float = 3.0
    read_timeout: float = 5.0
    max_retries: int = 3
    auth_header: str = "Authorization"
    tenant_header: str = "X-Tenant-Id"
    request_id_header: str = "X-Request-Id"

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("HttpVpnConfig.base_url 不能为空")
        if not self.tenant_id:
            raise ValueError("HttpVpnConfig.tenant_id 不能为空")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("HttpVpnConfig 超时必须 > 0")
        if self.max_retries < 1:
            raise ValueError("HttpVpnConfig.max_retries 必须 >= 1")
        if not self.tenant_header or not self.request_id_header:
            raise ValueError("HttpVpnConfig 的 tenant_header / request_id_header 不能为空")


def build_http_config_from_env() -> HttpVpnConfig:
    """从 VPN_HTTP_* 环境变量构造 HttpVpnConfig（real 模式构建期调用）。

    必填缺失或非法则抛 ValueError（在 build_vpn_adapter("real") 时触发），
    因此 settings.from_env 在 dev/test 未配置这些变量时仍能正常导入。
    """
    base_url = os.getenv("VPN_HTTP_BASE_URL", "").strip()
    tenant_id = os.getenv("VPN_TENANT_ID", "").strip()
    if not base_url:
        raise ValueError("real 模式缺少 VPN_HTTP_BASE_URL（真实只读 HTTP 数据源）")
    if not tenant_id:
        raise ValueError("real 模式缺少 VPN_TENANT_ID（真实只读 HTTP 数据源）")
    api_key = os.getenv("VPN_API_KEY", "").strip()

    def _float(name: str, default: float) -> float:
        raw = os.getenv(name, str(default)).strip()
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"环境变量 {name} 必须是数字") from exc

    def _int(name: str, default: int) -> int:
        raw = os.getenv(name, str(default)).strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"环境变量 {name} 必须是整数") from exc

    return HttpVpnConfig(
        base_url=base_url,
        api_key=api_key,
        tenant_id=tenant_id,
        connect_timeout=_float("VPN_HTTP_CONNECT_TIMEOUT", 3.0),
        read_timeout=_float("VPN_HTTP_READ_TIMEOUT", 5.0),
        max_retries=_int("VPN_HTTP_MAX_RETRIES", 3),
        auth_header=os.getenv("VPN_HTTP_AUTH_HEADER", "Authorization").strip() or "Authorization",
        tenant_header=os.getenv("VPN_HTTP_TENANT_HEADER", "X-Tenant-Id").strip() or "X-Tenant-Id",
        request_id_header=os.getenv("VPN_HTTP_REQUEST_ID_HEADER", "X-Request-Id").strip()
        or "X-Request-Id",
    )


class HttpReadonlyVpnAdapter(VpnAdapter):
    """真实只读 HTTP VPN 数据源适配器。只实现 6 个只读契约方法。

    - 绝不实现任何写方法：red line —— 本类上不存在 reissue_config / restart_gateway /
      modify_firewall / modify_vpn_config / unlock_account / grant_vpn_permission /
      toggle_gateway 等（写操作全部缺失，自动写不可能发生）。
    - 超时 → asyncio.TimeoutError（外层映射为 timeout，可重试）；
      连接/传输错误 → ConnectionError（外层映射为 transient，可重试）；
      4xx/5xx → 返回 {found:False, error_code, reason}（不抛异常，由调用方展示）。
    - 每个请求把 external_request_id（透传，缺省自生成）作为请求头发给厂商；
      结果回填 external_request_id，若响应带 X-Trace-Id 则回填 vendor_trace_id。

    用法：HttpReadonlyVpnAdapter(HttpVpnConfig(base_url=..., api_key=..., tenant_id=...))
    通常再被 VpnResilientAdapter 包裹以叠加 request_id / retry / 熔断 / 审计 / 脱敏。
    """

    # 供 VpnResilientAdapter 判定其支持 external_request_id 注入（保持 mock/sandbox 不变）。
    _accepts_external_request_id = True

    def __init__(
        self,
        config: HttpVpnConfig,
        *,
        request_id_factory: Callable[[], str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._new_request_id = request_id_factory or (lambda: uuid.uuid4().hex)
        self._last_vendor_trace: str | None = None
        if client is None:
            # 底层兜底超时（connect / read / write / pool）；外层 VpnResilientAdapter 还会再限制一次。
            timeout = httpx.Timeout(
                connect=config.connect_timeout,
                read=config.read_timeout,
                write=config.read_timeout,
                pool=config.connect_timeout,
            )
            client = httpx.AsyncClient(timeout=timeout)
        else:
            # 注入的 client（如 httpx.MockTransport）可能自带 timeout，不覆盖。
            timeout = httpx.Timeout(
                connect=config.connect_timeout,
                read=config.read_timeout,
                write=config.read_timeout,
                pool=config.connect_timeout,
            )
            client = client
        self._client = client
        self._timeout = timeout
        self._auth_value = f"Bearer {config.api_key}" if config.api_key else ""

    # ---- 契约：6 个只读方法（全部不带副作用，绝不写真实系统） ----

    async def get_account_status(
        self, user_id: str, *, external_request_id: str | None = None
    ) -> dict[str, Any]:
        return await self._get(
            "get_account_status",
            external_request_id=external_request_id,
            user_id=user_id,
        )

    async def get_client_config_version(
        self, user_id: str, *, external_request_id: str | None = None
    ) -> dict[str, Any]:
        return await self._get(
            "get_client_config_version",
            external_request_id=external_request_id,
            user_id=user_id,
        )

    async def get_gateway_status(
        self,
        gateway_id: str | None = None,
        region: str | None = None,
        *,
        external_request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            "get_gateway_status",
            external_request_id=external_request_id,
            gateway_id=gateway_id,
            region=region,
        )

    async def get_asset(
        self,
        asset_id: str | None = None,
        query: str = "",
        *,
        external_request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            "get_asset",
            external_request_id=external_request_id,
            asset_id=asset_id,
            query=query,
        )

    async def get_incident_status(
        self, incident_id: str, *, external_request_id: str | None = None
    ) -> dict[str, Any]:
        return await self._get(
            "get_incident_status",
            external_request_id=external_request_id,
            incident_id=incident_id,
        )

    async def get_similar_tickets(
        self,
        user_id: str,
        fault: str | None = None,
        *,
        external_request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            "get_similar_tickets",
            external_request_id=external_request_id,
            user_id=user_id,
            fault=fault,
        )

    # 红线：不实现 reissue_config —— 沿用基类 VpnAdapter.reissue_config 的 NotImplementedError。
    # 不实现任何写方法（restart_gateway / modify_* / unlock_account / grant_* 等）→ hasattr 为 False。

    # ---- 内部：统一的 HTTP GET + 解析 ----

    async def _get(self, method: str, *, external_request_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        request_id = external_request_id or self._new_request_id()
        url, params = self._build_url(method, **kwargs)
        headers = self._headers(request_id)
        try:
            resp = await self._client.get(url, params=params or None, headers=headers)
        except httpx.TimeoutException as exc:
            # 外层 VpnResilientAdapter 的 default_error_mapper 识别 TimeoutError → timeout（可重试）。
            raise TimeoutError(f"VPN HTTP 数据源超时: {method}") from exc
        except httpx.TransportError as exc:
            # 外层映射为 transient（可重试）。
            raise ConnectionError(f"VPN HTTP 数据源连接异常: {method}") from exc

        self._last_vendor_trace = resp.headers.get("X-Trace-Id")
        # 4xx/5xx：不抛异常，返回结构化错误 dict。
        if resp.status_code >= 400:
            return self._error(request_id, "http_error", f"HTTP {resp.status_code}", reason_from_status(resp.status_code))
        try:
            data = resp.json()
        except ValueError:
            return self._error(request_id, "invalid_response", "厂商返回非 JSON", reason="响应体不是有效 JSON")
        if not isinstance(data, dict):
            return self._error(request_id, "invalid_response", "厂商返回非对象", reason="响应体不是 JSON 对象")
        result = self._parse(method, data, **kwargs)
        return self._add_trace(result, request_id)

    def _headers(self, request_id: str) -> dict[str, str]:
        headers: dict[str, str] = {
            self._config.tenant_header: self._config.tenant_id,
            self._config.request_id_header: request_id,
        }
        if self._auth_value:
            headers[self._config.auth_header] = self._auth_value
        return headers

    def _build_url(self, method: str, **kwargs: Any) -> tuple[str, dict[str, str]]:
        """按方法构造 URL + 查询参数（子类可 override 以对齐真实端点）。"""
        base = self._config.base_url.rstrip("/")
        if method == "get_gateway_status":
            gateway_id = kwargs.get("gateway_id")
            region = kwargs.get("region")
            if gateway_id:
                return f"{base}/gateways/{gateway_id}", {"region": region} if region else {}
            return f"{base}/gateways", {"region": region} if region else {}
        if method == "get_asset":
            asset_id = kwargs.get("asset_id")
            query = kwargs.get("query") or ""
            if asset_id:
                return f"{base}/assets/{asset_id}", {}
            return f"{base}/assets", {"query": query} if query else {}
        template = _ENDPOINTS[method]
        path = template.format(**{k: kwargs.get(k) for k in ("user_id", "incident_id")})
        params: dict[str, str] = {}
        if method == "get_similar_tickets" and kwargs.get("fault"):
            params["fault"] = kwargs["fault"]
        return f"{base}{path}", params

    # ---- 解析：把厂商 JSON 归一化为 VpnAdapter 契约形状 ----

    def _parse(self, method: str, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        parser = getattr(self, f"_parse_{method}", None)
        if parser is None:
            return {"found": False, "content": f"数据源未实现解析 {method}"}
        return parser(data, **kwargs)

    def _parse_get_account_status(self, data: dict[str, Any], user_id: str) -> dict[str, Any]:
        if not self._found(data, keys=("user_id", "status")):
            return self._not_found(data, f"未找到用户 {user_id} 的 VPN 账号记录", user_id=user_id)
        record = dict(data)
        record.setdefault("user_id", user_id)
        record.setdefault("content", f"账号 {user_id} 状态={data.get('status', 'unknown')}, "
                                    f"过期={data.get('expires_at', '未知')}, 角色={data.get('role', '未知')}")
        return record

    def _parse_get_client_config_version(self, data: dict[str, Any], user_id: str) -> dict[str, Any]:
        if not self._found(data, keys=("version",)):
            return self._not_found(data, f"未找到用户 {user_id} 的客户端配置版本", user_id=user_id)
        record = dict(data)
        record.setdefault("user_id", user_id)
        record.setdefault("content", f"客户端配置版本 user={user_id} version={data.get('version', '未知')} "
                                    f"生成于 {data.get('generated_at', '未知')}")
        return record

    def _parse_get_gateway_status(
        self, data: dict[str, Any], gateway_id: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        if not self._found(data, keys=("gateway_id", "status")):
            return self._not_found(data, "未找到匹配的网关", gateway_id=gateway_id, region=region)
        record = dict(data)
        record.setdefault("gateway_id", data.get("gateway_id") or gateway_id)
        record.setdefault("content", f"网关 {data.get('gateway_id')} region={data.get('region', '未知')} "
                                    f"status={data.get('status', 'unknown')}")
        return record

    def _parse_get_asset(
        self, data: dict[str, Any], asset_id: str | None = None, query: str = ""
    ) -> dict[str, Any]:
        if not self._found(data, keys=("asset_id", "hostname", "name")):
            return self._not_found(data, "未找到匹配的资产", asset_id=asset_id, query=query)
        record = dict(data)
        record.setdefault("asset_id", data.get("asset_id") or asset_id)
        record.setdefault("content", f"资产：{data.get('asset_id')} {data.get('hostname') or data.get('name') or ''} "
                                    f"({data.get('asset_type')}) 状态={data.get('status')} "
                                    f"归属={data.get('owner_user_id') or '无'}")
        return record

    def _parse_get_incident_status(self, data: dict[str, Any], incident_id: str) -> dict[str, Any]:
        if not self._found(data, keys=("incident_id", "status")):
            return self._not_found(data, f"未找到事件 {incident_id}", incident_id=incident_id)
        record = dict(data)
        record.setdefault("incident_id", incident_id)
        record.setdefault("content", f"事件 {incident_id} status={data.get('status', 'unknown')} "
                                    f"severity={data.get('severity', 'unknown')}")
        return record

    def _parse_get_similar_tickets(
        self, data: dict[str, Any], user_id: str, fault: str | None = None
    ) -> dict[str, Any]:
        tickets = list(data.get("tickets") or []) if isinstance(data.get("tickets"), list) else []
        if not self._found(data, keys=("tickets",)) and not tickets:
            return self._not_found(data, "没有找到相似的历史工单", user_id=user_id, fault=fault)
        lines = [
            f"- #{t.get('ticket_id')} [{t.get('status', '?')}] {t.get('category', '?')} | {t.get('title', '')}"
            f"（{t.get('resolved_at') or '未解决'}）"
            for t in tickets[:20]
        ]
        record = dict(data)
        record.setdefault("tickets", tickets)
        record.setdefault("user_id", user_id)
        record.setdefault("fault", fault)
        record.setdefault("content", "历史相似工单：\n" + "\n".join(lines) if lines else "没有找到相似的历史工单")
        return record

    # ---- 工具方法 ----

    @staticmethod
    def _found(data: dict[str, Any], keys: tuple[str, ...]) -> bool:
        if "found" in data:
            return bool(data.get("found"))
        return any(data.get(k) for k in keys)

    @staticmethod
    def _not_found(data: dict[str, Any], content: str, **kwargs: Any) -> dict[str, Any]:
        out = {"found": False, "content": content}
        out.update(kwargs)
        if data.get("content_display"):
            out["content"] = data["content_display"]
        return out

    @staticmethod
    def _error(request_id: str, code: str, content: str, reason: str) -> dict[str, Any]:
        return {
            "found": False,
            "error_code": code,
            "reason": reason,
            "content": content,
            "external_request_id": request_id,
        }

    def _add_trace(self, result: dict[str, Any], request_id: str) -> dict[str, Any]:
        result["external_request_id"] = request_id
        if self._last_vendor_trace:
            result["vendor_trace_id"] = self._last_vendor_trace
        return result


def reason_from_status(status: int) -> str:
    """把 HTTP 状态码映射为简短原因（供结构化错误）。"""
    if status == 401:
        return "鉴权失败：API 凭据无效"
    if status == 403:
        return "授权受限：租户无访问权限"
    if status == 404:
        return "资源不存在"
    if status == 429:
        return "请求被限流"
    if 500 <= status < 600:
        return "厂商服务端错误"
    return f"HTTP {status}"
