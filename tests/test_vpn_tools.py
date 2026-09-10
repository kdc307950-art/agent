"""VPN Diagnosis Agent 6 个只读工具单元测试（backend/vpn/tools.py）。

覆盖契约 §4 + 验收「工具调用全部过治理/租户隔离/超时/审计/白名单」：
    - 6 个工具注入 runtime + RunContext 后可调用，返回可解析的 {"content", ...} JSON 字符串；
    - 无 RunContext / 缺少租户上下文时被拒绝（抛错或返回错误 JSON）；
    - 覆盖「伪造 send_message / 未注册工具 / 掉 scope」的模型响应经
      ToolGovernance 白名单与 scope 校验被拒绝（不执行 + 不产生副作用）；
    - 租户隔离：工具实现用 runtime.context.tenant_id，不信任入参。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from backend.run_context import RunContext
from backend.tool_governance import DEFAULT_TOOL_POLICIES, VPN_DIAGNOSIS_TOOLS, ToolGovernance
from backend.vpn import VPN_TOOLS, MockVpnAdapter


class _FakeAudit:
    def __init__(self):
        self.events = []

    async def record_event(self, context, event_type, **kwargs):
        self.events.append((context.run_id, event_type, kwargs))


def _context(
    tenant_id: str = "tenant-a",
    scopes=frozenset({"ticket:agent"}),
    allowed_tools=VPN_DIAGNOSIS_TOOLS,
) -> RunContext:
    return RunContext(
        run_id="run-vpn",
        request_id="req-1",
        tenant_id=tenant_id,
        user_id="user-1",
        thread_id=f"vpn:{tenant_id}:t-1",
        scopes=scopes,
        deadline=time.time() + 60,
        allowed_tools=allowed_tools,
    )


def _runtime(tenant_id: str = "tenant-a", adapter: Any | None = None):
    return SimpleNamespace(
        context=_context(tenant_id),
        vpn_adapter=adapter or MockVpnAdapter(),
    )


def _call_tool(tool, args: dict, runtime):
    async def run():
        cfg = {"configurable": {"runtime": runtime}}
        coroutine = getattr(tool, "coroutine", None)
        if coroutine is not None:
            return await coroutine(**args, config=cfg)
        return await tool.ainvoke(args, config=cfg)

    return asyncio.run(run())


def _find(name: str):
    return next(t for t in VPN_TOOLS if t.name == name)


def _governance_call(request):
    async def run():
        audit = _FakeAudit()
        governance = ToolGovernance(audit, policies=DEFAULT_TOOL_POLICIES)
        executed = False

        async def execute(_request):
            nonlocal executed
            executed = True
            return "should not run"

        result = await governance.awrap_tool_call(request, execute)
        return result, executed, audit.events

    return asyncio.run(run())


# ========== 6 个工具：可调用 + 返回 {content, ...} JSON ==========


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("search_vpn_knowledge", {"query": "VPN 连接失败"}),
        ("get_asset", {"asset_id": "asset-001"}),
        ("get_vpn_account_status", {"user_id": "user-042"}),
        ("get_vpn_gateway_status", {"gateway_id": "gw-cn-north"}),
        ("get_recent_similar_tickets", {"user_id": "user-042"}),
        ("get_incident_status", {"incident_id": "INC-9"}),
    ],
)
def test_each_tool_callable_with_runtime_and_returns_json(tool_name, args):
    runtime = _runtime()
    result = _call_tool(_find(tool_name), args, runtime)
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert isinstance(parsed, dict)
    assert "content" in parsed


# ========== 无 RunContext / 缺租户上下文被拒绝 ==========


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("search_vpn_knowledge", {"query": "vpn"}),
        ("get_asset", {"asset_id": "asset-001"}),
        ("get_vpn_account_status", {"user_id": "user-042"}),
        ("get_vpn_gateway_status", {"gateway_id": "gw-cn-north"}),
        ("get_recent_similar_tickets", {"user_id": "user-042"}),
        ("get_incident_status", {"incident_id": "INC-9"}),
    ],
)
def test_tool_rejected_without_run_context(tool_name, args):
    with pytest.raises(RuntimeError):
        _call_tool(_find(tool_name), args, None)


def test_tool_rejected_when_tenant_missing():
    runtime = _runtime(tenant_id="")
    result = _call_tool(_find("search_vpn_knowledge"), {"query": "vpn"}, runtime)
    assert "缺少租户上下文" in json.loads(result).get("content", "")


# ========== 治理：scope 校验 / 白名单 / 未注册 / 伪副作用（验收核心） ==========


def test_governance_denies_vpn_tool_without_ticket_agent_scope():
    context = _context(scopes=frozenset({"chat:write"}))  # 无 ticket:agent
    request = SimpleNamespace(
        tool_call={
            "name": "search_vpn_knowledge",
            "args": {"query": "vpn"},
            "id": "c1",
            "type": "tool_call",
        },
        tool=object(),
        runtime=SimpleNamespace(context=context),
    )
    result, executed, events = _governance_call(request)
    assert result.status == "error"
    assert "权限" in result.content
    assert executed is False
    assert events[0][1] == "tool_call_denied"


def test_governance_denies_forged_send_message_via_allowlist():
    """模型伪造 send_message：allowed_tools profile 排除则拒绝（零副作用）。"""
    context = _context(allowed_tools=VPN_DIAGNOSIS_TOOLS)
    request = SimpleNamespace(
        tool_call={
            "name": "send_message",
            "args": {"content": "hi"},
            "id": "c1",
            "type": "tool_call",
        },
        tool=object(),
        runtime=SimpleNamespace(context=context),
    )
    result, executed, events = _governance_call(request)
    assert result.status == "error"
    assert "未启用" in result.content
    assert executed is False
    assert events[0][1] == "tool_call_denied"


def test_governance_denies_unregistered_tool():
    context = _context()
    request = SimpleNamespace(
        tool_call={"name": "restart_gateway", "args": {}, "id": "c1", "type": "tool_call"},
        tool=object(),
        runtime=SimpleNamespace(context=context),
    )
    result, executed, _events = _governance_call(request)
    assert result.status == "error"
    assert executed is False


def test_governance_tool_success_is_audited():
    context = _context()
    request = SimpleNamespace(
        tool_call={
            "name": "search_vpn_knowledge",
            "args": {"query": "vpn"},
            "id": "c1",
            "type": "tool_call",
        },
        tool=object(),
        runtime=SimpleNamespace(context=context),
    )
    audit = _FakeAudit()
    governance = ToolGovernance(audit, policies=DEFAULT_TOOL_POLICIES)

    async def execute(_request):
        return '{"content": "知识库命中：vpn-001"}'

    result = asyncio.run(governance.awrap_tool_call(request, execute))
    assert "知识库命中" in result
    types = [e[1] for e in audit.events]
    assert "tool_call_started" in types
    assert "tool_call_completed" in types


# ========== 租户隔离：工具从 RunContext 取 tenant，不信任入参 ==========


def test_tool_reads_tenant_from_context_not_input():
    """工具从 RunContext 取租户；缺少租户上下文即拒绝（不信任入参携带身份）。"""
    runtime = _runtime(tenant_id="")
    result = _call_tool(_find("get_asset"), {"asset_id": "asset-001"}, runtime)
    assert "缺少租户上下文" in json.loads(result).get("content", "")
