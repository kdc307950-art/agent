"""LANGGraph VPN Diagnosis Agent — 6 个只读诊断工具（统一 {content, ...} JSON 契约）。

模块归属：backend/vpn。设计要点（对齐 docs/product/vpn-diagnosis-agent-contract.md）：
    - 6 个工具：search_vpn_knowledge / get_asset / get_vpn_account_status /
      get_vpn_gateway_status / get_recent_similar_tickets / get_incident_status。
    - 全部 side_effect=False、scope=ticket:agent、租户从 RunContext 取（不信任入参）。
    - 统一返回 JSON 字符串 {"content": 展示文本, ...}（content 给模型/坐席看，
      与 copilot.tools.search_knowledge 的 {content, evidence} 契约对齐）。
    - 数据源统一走 runtime.vpn_adapter（MockVpnAdapter / 未来真实适配器），
      不在工具层耦合真实网关 SDK，满足「不做真实 VPN 自动诊断」。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from .approval import (
    ReissueActionRequest,
    build_reissue_idempotency_key,
    execute_approved_reissue,
)


def _runtime(config: RunnableConfig | None) -> Any:
    return (config or {}).get("configurable", {}).get("runtime")


def _run_context(config: RunnableConfig | None) -> Any:
    runtime = _runtime(config)
    if runtime is None or not hasattr(runtime, "context"):
        raise RuntimeError("工具缺少服务端运行上下文")
    return runtime.context


def _vpn_adapter(config: RunnableConfig | None) -> Any:
    runtime = _runtime(config)
    # 优先 runtime.vpn_adapter（Mock/真实适配器统一入口）
    adapter = getattr(runtime, "vpn_adapter", None)
    return adapter


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


@tool
async def search_vpn_knowledge(
    query: str,
    *,
    limit: int = 5,
    config: RunnableConfig | None = None,
) -> str:
    """搜索 VPN 知识库（只读），返回 {"content": 展示文本, "evidence": [...]}。

    数据源：runtime.vpn_adapter.search_knowledge；租户由 RunContext 提供，不信任入参。
    """
    if not query or not query.strip() or len(query) > 1_024:
        return _json({"content": "错误：查询不能为空且不能超过 1024 字符", "evidence": []})
    if limit < 1 or limit > 20:
        return _json({"content": "错误：limit 必须在 1 到 20 之间", "evidence": []})
    # 强制读取租户（tenant_id 必须存在，否则拒绝）
    context = _run_context(config)
    tenant_id = getattr(context, "tenant_id", None)
    if not tenant_id:
        return _json({"content": "错误：缺少租户上下文", "evidence": []})
    adapter = _vpn_adapter(config)
    if adapter is None:
        return _json({"content": "错误：VPN 数据源未配置", "evidence": []})
    result = await adapter.search_knowledge(query, limit=limit)
    return _json(
        {
            "content": result.get("content", ""),
            "evidence": result.get("evidence", []),
            "found": result.get("found", False),
        }
    )


@tool
async def get_asset(
    asset_id: str | None = None,
    *,
    query: str = "",
    limit: int = 10,
    config: RunnableConfig | None = None,
) -> str:
    """查询 IT 资产生命周期状态（只读），返回 {"content": 资产文本, ...}。

    asset_id 或 query 提供其一；数据源：runtime.vpn_adapter.get_asset。
    """
    if limit < 1 or limit > 50:
        return _json({"content": "错误：limit 必须在 1 到 50 之间"})
    if not asset_id and not (query or "").strip():
        return _json({"content": "错误：必须提供 asset_id 或 query"})
    context = _run_context(config)
    tenant_id = getattr(context, "tenant_id", None)
    if not tenant_id:
        return _json({"content": "错误：缺少租户上下文"})
    adapter = _vpn_adapter(config)
    if adapter is None:
        return _json({"content": "错误：VPN 数据源未配置"})
    result = await adapter.get_asset(asset_id=asset_id, query=query)
    return _json({"content": result.get("content", ""), "found": result.get("found", False)})


@tool
async def get_vpn_account_status(
    user_id: str,
    *,
    config: RunnableConfig | None = None,
) -> str:
    """查询 VPN 账号状态（只读），返回 {"content": 账号状态文本, ...}。

    数据源：runtime.vpn_adapter.get_account_status；租户从 RunContext 取。
    """
    if not user_id or len(user_id) > 128:
        return _json({"content": "错误：user_id 不能为空且不能超过 128 字符"})
    context = _run_context(config)
    tenant_id = getattr(context, "tenant_id", None)
    if not tenant_id:
        return _json({"content": "错误：缺少租户上下文"})
    adapter = _vpn_adapter(config)
    if adapter is None:
        return _json({"content": "错误：VPN 数据源未配置"})
    result = await adapter.get_account_status(user_id)
    return _json({"content": result.get("content", ""), "found": result.get("found", False)})


@tool
async def get_vpn_gateway_status(
    gateway_id: str | None = None,
    *,
    region: str | None = None,
    config: RunnableConfig | None = None,
) -> str:
    """查询 VPN 网关运行状态（只读），返回 {"content": 网关状态文本, ...}。

    gateway_id 或 region 提供其一；数据源：runtime.vpn_adapter.get_gateway_status。
    """
    if not gateway_id and not region:
        return _json({"content": "错误：必须提供 gateway_id 或 region"})
    context = _run_context(config)
    tenant_id = getattr(context, "tenant_id", None)
    if not tenant_id:
        return _json({"content": "错误：缺少租户上下文"})
    adapter = _vpn_adapter(config)
    if adapter is None:
        return _json({"content": "错误：VPN 数据源未配置"})
    result = await adapter.get_gateway_status(gateway_id=gateway_id, region=region)
    return _json({"content": result.get("content", ""), "found": result.get("found", False)})


@tool
async def get_recent_similar_tickets(
    user_id: str,
    *,
    fault: str | None = None,
    limit: int = 5,
    config: RunnableConfig | None = None,
) -> str:
    """查询某客户相似历史工单（只读），返回 {"content": 历史工单文本, ...}。

    fault 可选（对齐 VPN_FAULT_* 枚举值）；数据源：runtime.vpn_adapter.get_similar_tickets。
    """
    if not user_id or len(user_id) > 128:
        return _json({"content": "错误：user_id 不能为空且不能超过 128 字符"})
    if limit < 1 or limit > 20:
        return _json({"content": "错误：limit 必须在 1 到 20 之间"})
    context = _run_context(config)
    tenant_id = getattr(context, "tenant_id", None)
    if not tenant_id:
        return _json({"content": "错误：缺少租户上下文"})
    adapter = _vpn_adapter(config)
    if adapter is None:
        return _json({"content": "错误：VPN 数据源未配置"})
    result = await adapter.get_similar_tickets(user_id, fault=fault)
    return _json(
        {
            "content": result.get("content", ""),
            "found": result.get("found", False),
            "tickets": result.get("tickets", []),
            "fault": fault,
        }
    )


@tool
async def get_incident_status(
    incident_id: str,
    *,
    config: RunnableConfig | None = None,
) -> str:
    """查询事件/升级单状态（只读），返回 {"content": 事件状态文本, ...}。

    数据源：runtime.vpn_adapter.get_incident_status。
    """
    if not incident_id or len(incident_id) > 64:
        return _json({"content": "错误：incident_id 不能为空且不能超过 64 字符"})
    context = _run_context(config)
    tenant_id = getattr(context, "tenant_id", None)
    if not tenant_id:
        return _json({"content": "错误：缺少租户上下文"})
    adapter = _vpn_adapter(config)
    if adapter is None:
        return _json({"content": "错误：VPN 数据源未配置"})
    result = await adapter.get_incident_status(incident_id)
    return _json({"content": result.get("content", ""), "found": result.get("found", False)})


@tool
async def get_client_config_version(
    user_id: str,
    *,
    config: RunnableConfig | None = None,
) -> str:
    """查询某用户的 VPN 客户端配置版本（只读），返回 {"content": 版本文本, "version": ...}。

    数据源：runtime.vpn_adapter.get_client_config_version；供「重新下发配置」前置校验使用。
    """
    if not user_id or len(user_id) > 128:
        return _json({"content": "错误：user_id 不能为空且不能超过 128 字符"})
    context = _run_context(config)
    tenant_id = getattr(context, "tenant_id", None)
    if not tenant_id:
        return _json({"content": "错误：缺少租户上下文"})
    adapter = _vpn_adapter(config)
    if adapter is None:
        return _json({"content": "错误：VPN 数据源未配置"})
    result = await adapter.get_client_config_version(user_id)
    return _json(
        {
            "content": result.get("content", ""),
            "found": result.get("found", False),
            "version": result.get("version"),
        }
    )


@tool
async def reissue_vpn_config(
    user_id: str,
    *,
    asset_id: str | None = None,
    ticket_id: str = "",
    client_version: str = "",
    idempotency_key: str = "",
    config: RunnableConfig | None = None,
) -> str:
    """重新下发 VPN 客户端配置（受控副作用动作；未获批准不执行）。

    该工具由审批式执行链路（backend/vpn/approval）驱动：
    - 必须已存在 APPROVED 登记（approve_reissue 触发）且前置校验通过、幂等键未执行过，
      否则返回 not_approved / preflight_failed，绝不产生副作用。
    - 不作为只读诊断工具加入 VPN_DIAGNOSIS_TOOLS（只读 Agent 不会调用它）；
      治理层以 side_effect=True 策略注册，未授权路径解析即被拒。
    """
    if not user_id or len(user_id) > 128:
        return _json({"error_code": "invalid_input", "reason": "user_id 非法"})
    if not client_version:
        return _json({"error_code": "invalid_input", "reason": "缺少目标 client_version"})
    context = _run_context(config)
    tenant_id = getattr(context, "tenant_id", None)
    if not tenant_id:
        return _json({"error_code": "missing_tenant", "reason": "缺少租户上下文"})
    runtime = _runtime(config)
    if runtime is None:
        return _json({"error_code": "no_runtime", "reason": "运行时未配置"})
    key = idempotency_key or build_reissue_idempotency_key(
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=client_version,
    )
    request = ReissueActionRequest(
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=client_version,
        idempotency_key=key,
        expected_version=0,
    )
    result = await execute_approved_reissue(request=request, runtime=runtime, run_context=context)
    return _json(result.model_dump(mode="json"))


# 只读诊断工具集合（VPN_DIAGNOSIS_TOOLS 对应的 @tool 定义：全部 side_effect=False）。
VPN_TOOLS = [
    search_vpn_knowledge,
    get_asset,
    get_vpn_account_status,
    get_vpn_gateway_status,
    get_recent_similar_tickets,
    get_incident_status,
    get_client_config_version,
]

# 受控审批执行工具集合（side_effect=True；不进 VPN_DIAGNOSIS_TOOLS，
# 只读 Agent 不会绑定/调用；治理层以此注册 side_effect 策略与执行 profile）。
VPN_REISSUE_TOOLS = [
    reissue_vpn_config,
]
