"""VPN Diagnosis Agent 有界工具循环单元测试（backend/vpn/agent.py）。

覆盖契约 §6 + 验收「agent 无法直接改工单、无法执行副作用」：
    - 有界循环：模拟模型反复调用工具，断言轮次/工具数/超时硬限制终止；
    - 模型输出转成结构化 DiagnosisCommand；
    - 禁止命令/伪造副作用工具不会被 agent 产出或执行（经治理拒绝）；
    - 无命令/超限/超时 -> must_handoff=True（上层转人工）。

模型相关测试不使用真实 API key（桩模型 + 桩工具 + 人造 RunContext）。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage

from backend.run_context import RunContext
from backend.tool_governance import VPN_DIAGNOSIS_TOOLS, ToolGovernance
from backend.vpn import (
    DiagnosisLimits,
    DiagnosisRequest,
    MockVpnAdapter,
    VpnDiagnosisAgent,
)


class _StubTool:
    """可 ainvoke 的桩工具：记录调用次数，按脚本返回结果。"""

    def __init__(self, name: str, results: list[Any] | None = None):
        self.name = name
        self.calls: list[dict] = []
        self.results = list(results or ["ok"])

    async def ainvoke(self, args: dict, config=None):
        self.calls.append(args)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


class _SleepTool(_StubTool):
    """调用即阻塞的桩工具：用于触发治理层单工具超时。"""

    async def ainvoke(self, args: dict, config=None):
        self.calls.append(args)
        await asyncio.sleep(0.5)
        return "late"


class _CommandModel:
    """桩模型：先发起工具调用（可选），再输出结构化命令 JSON。"""

    def __init__(
        self,
        tool_names: list[str] | None = None,
        final_command: dict | None = None,
        rounds_before_final: int = 1,
    ):
        self.tool_names = tool_names or []
        self.final_command = final_command or {
            "command": "provide_steps",
            "content": "请重启 VPN 客户端",
            "payload": {"ticket_id": "t-1"},
            "reason_codes": ["gate_passed"],
            "confidence": 0.95,
        }
        self.rounds_before_final = rounds_before_final
        self.round = 0

    async def ainvoke(self, messages, config=None):
        self.round += 1
        if self.round <= self.rounds_before_final and self.tool_names:
            return AIMessage(
                content="需要诊断",
                tool_calls=[
                    {"name": name, "args": {"query": "vpn"}, "id": f"call-{i}", "type": "tool_call"}
                    for i, name in enumerate(self.tool_names)
                ],
            )
        return AIMessage(content=json.dumps(self.final_command, ensure_ascii=False))


class _AlwaysToolCallModel:
    """每轮都发起工具调用的模型：验证上限终止。"""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self.round = 0

    async def ainvoke(self, messages, config=None):
        self.round += 1
        return AIMessage(
            content="继续",
            tool_calls=[
                {"name": self.tool_name, "args": {"query": "x"}, "id": f"call-{self.round}", "type": "tool_call"}
            ],
        )


class _FakeAudit:
    def __init__(self):
        self.events = []

    async def record_event(self, context, event_type, **kwargs):
        self.events.append((context.run_id, event_type, kwargs))


class _FakeMetrics:
    def __init__(self):
        self.counts: dict[str, int] = {}

    def increment(self, name: str, amount: int = 1, attributes=None):
        self.counts[name] = self.counts.get(name, 0) + (amount or 1)


def _runtime(*, scopes=frozenset({"ticket:agent"}), allowed_tools=VPN_DIAGNOSIS_TOOLS, tenant_id="tenant-a"):
    context = RunContext(
        run_id="run-vpn-agent",
        request_id="req-1",
        tenant_id=tenant_id,
        user_id="user-1",
        thread_id=f"vpn:{tenant_id}:t-1",
        scopes=scopes,
        deadline=time.time() + 60,
        allowed_tools=allowed_tools,
    )
    audit = _FakeAudit()
    return SimpleNamespace(
        context=context,
        tool_governance=ToolGovernance(audit),
        audit=audit,
        metrics=_FakeMetrics(),
        vpn_adapter=MockVpnAdapter(),
    )


def _request(**overrides) -> DiagnosisRequest:
    base = {
        "ticket_id": "t-1",
        "requester_id": "user-1",
        "tenant_id": "tenant-a",
        "ticket_text": "VPN 无法连接，需要排查",
        "fault": "connection_failed",
        "current_status": "assigned",
        "identity_ok": True,
    }
    base.update(overrides)
    return DiagnosisRequest(**base)


def _agent(tools: dict[str, Any], model, limits: DiagnosisLimits | None = None) -> VpnDiagnosisAgent:
    return VpnDiagnosisAgent(model=model, tools=tools, limits=limits)


def _run(agent, request, runtime):
    return asyncio.run(agent.run(request, runtime=runtime, run_context=runtime.context))


# ========== 模型输出 -> 结构化 DiagnosisCommand ==========


def test_agent_produces_structured_command():
    tool = _StubTool("search_vpn_knowledge")
    runtime = _runtime()

    async def run():
        agent = _agent(
            {"search_vpn_knowledge": tool},
            _CommandModel(["search_vpn_knowledge"]),
        )
        return await agent.run(_request(), runtime=runtime, run_context=runtime.context)

    result = asyncio.run(run())
    # 模型输出被解析为结构化 DiagnosisCommand（Agent 只产出命令，不改工单）
    assert result["command"]["command"] == "provide_steps"
    assert result["command"]["confidence"] == 0.95
    assert len(tool.calls) == 1
    assert result["tool_trace"][0]["tool"] == "search_vpn_knowledge"
    assert result["tool_trace"][0]["status"] == "completed"


# ========== 有界循环：工具数/轮次/超时硬限制 ==========


def test_tool_call_limit_terminates_run():
    tool = _StubTool("search_vpn_knowledge")
    runtime = _runtime()

    async def run():
        agent = _agent(
            {"search_vpn_knowledge": tool},
            _AlwaysToolCallModel("search_vpn_knowledge"),
            limits=DiagnosisLimits(max_rounds=10, max_tool_calls=2, max_tool_calls_per_round=2),
        )
        return await agent.run(_request(), runtime=runtime, run_context=runtime.context)

    result = asyncio.run(run())
    assert len(tool.calls) <= 2
    assert result["error_code"] == "tool_call_limit_exceeded"
    assert result["must_handoff"] is True


def test_round_limit_terminates_run():
    tool = _StubTool("search_vpn_knowledge")
    runtime = _runtime()

    async def run():
        agent = _agent(
            {"search_vpn_knowledge": tool},
            _AlwaysToolCallModel("search_vpn_knowledge"),
            limits=DiagnosisLimits(max_rounds=1, max_tool_calls=10, max_tool_calls_per_round=1),
        )
        return await agent.run(_request(), runtime=runtime, run_context=runtime.context)

    result = asyncio.run(run())
    assert result["error_code"] == "round_limit_exceeded"
    assert result["must_handoff"] is True


def test_single_tool_timeout_is_captured_not_fatal():
    """单工具超时被治理层捕获：工具轨迹标 timeout，主流程不崩。"""
    from backend.tool_governance import ToolPolicy

    async def run():
        tool = _SleepTool("search_vpn_knowledge")
        context = RunContext(
            run_id="run-vpn-agent",
            request_id="req-1",
            tenant_id="tenant-a",
            user_id="user-1",
            thread_id="vpn:tenant-a:t-1",
            scopes=frozenset({"ticket:agent"}),
            deadline=time.time() + 60,
            allowed_tools=VPN_DIAGNOSIS_TOOLS,
        )
        audit = _FakeAudit()
        # 用短超时策略模拟治理层单工具超时（不依赖真实模型/网络）
        governance = ToolGovernance(
            audit,
            policies={
                "search_vpn_knowledge": ToolPolicy(
                    name="search_vpn_knowledge",
                    required_scopes=frozenset({"ticket:agent"}),
                    timeout_seconds=0.01,
                    max_input_chars=1_024,
                    retryable=True,
                    side_effect=False,
                )
            },
        )
        runtime = SimpleNamespace(
            context=context,
            tool_governance=governance,
            audit=audit,
            metrics=_FakeMetrics(),
            vpn_adapter=MockVpnAdapter(),
        )
        agent = _agent(
            {"search_vpn_knowledge": tool},
            _CommandModel(
                ["search_vpn_knowledge"],
                final_command={"command": "ask_customer", "content": "请提供错误码", "confidence": 0.9},
            ),
            limits=DiagnosisLimits(single_tool_timeout_seconds=0.01),
        )
        return await agent.run(_request(), runtime=runtime, run_context=runtime.context)

    result = asyncio.run(run())
    assert result["tool_trace"][0]["status"] == "timeout"


def test_evidence_found_allows_non_handoff_command():
    """有依据（工具返回 found:true）+身份齐全+非群体+置信度达标 => 不转人工。

    修复前 governed_invoke._parse_evidence 只认 copilot 的 search_knowledge，VPN 工具
    的 {found, evidence} 被丢弃，导致恒 no_evidence -> 恒转人工。断言非转人工路径可用。
    """
    tool = _StubTool(
        "search_vpn_knowledge",
        results=[
            json.dumps(
                {
                    "content": "知识库命中：vpn-001",
                    "found": True,
                    "evidence": [{"document_id": "vpn-001", "title": "VPN 连接失败排查"}],
                },
                ensure_ascii=False,
            )
        ],
    )
    runtime = _runtime()

    async def run():
        agent = _agent(
            {"search_vpn_knowledge": tool},
            _CommandModel(
                ["search_vpn_knowledge"],
                final_command={
                    "command": "provide_steps",
                    "content": "请检查客户端版本与网关负载",
                    "payload": {"ticket_id": "t-1"},
                    "reason_codes": ["gate_passed"],
                    "confidence": 0.9,
                },
            ),
        )
        return await agent.run(_request(), runtime=runtime, run_context=runtime.context)

    result = asyncio.run(run())
    assert result["command"]["command"] == "provide_steps"
    assert result["must_handoff"] is False
    assert result["evaluation"]["must_handoff"] is False
    assert result["evaluation"]["handoff_reasons"] == []


# ========== 无命令/超限 => must_handoff（转人工） ==========


def test_no_command_and_no_error_forces_must_handoff():
    """模型永不产出命令（每轮只调工具）且未 error：round_limit 触发必转人工。"""
    tool = _StubTool("search_vpn_knowledge")
    runtime = _runtime()

    async def run():
        agent = _agent(
            {"search_vpn_knowledge": tool},
            _AlwaysToolCallModel("search_vpn_knowledge"),
            limits=DiagnosisLimits(max_rounds=1, max_tool_calls=10, max_tool_calls_per_round=1),
        )
        return await agent.run(_request(), runtime=runtime, run_context=runtime.context)

    result = asyncio.run(run())
    assert result["command"] is None
    assert result["must_handoff"] is True
    assert result["evaluation"]["must_handoff"] is True


# ========== 禁止命令不被 agent 产出 ==========


def test_forbidden_command_cannot_be_produced_by_agent():
    """模型输出 reset_password（禁止命令）：模型校验拒绝 -> 不产出命令 -> 转人工。"""
    tool = _StubTool("search_vpn_knowledge")
    runtime = _runtime()
    forged = {
        "command": "reset_password",
        "content": "重置密码",
        "payload": {},
        "reason_codes": [],
        "confidence": 0.99,
    }

    async def run():
        agent = _agent(
            {"search_vpn_knowledge": tool},
            _CommandModel([], final_command=forged, rounds_before_final=0),
        )
        return await agent.run(_request(), runtime=runtime, run_context=runtime.context)

    result = asyncio.run(run())
    assert result["command"] is None
    assert result["must_handoff"] is True


# ========== 伪造副作用工具经治理拒绝（验收核心） ==========


def test_forged_send_message_tool_is_denied_by_governance():
    """模型请求 send_message：即使工具在集合内，allowed_tools profile 排除 => 拒绝且零执行。"""
    # send_message 工具「在集合内」但 RunContext.allowed_tools 只放行只读工具
    send_tool = _StubTool("send_message")
    runtime = _runtime(allowed_tools=VPN_DIAGNOSIS_TOOLS)

    async def run():
        agent = _agent(
            {"send_message": send_tool},
            _AlwaysToolCallModel("send_message"),
            limits=DiagnosisLimits(max_rounds=1, max_tool_calls=1, max_tool_calls_per_round=1),
        )
        result = await agent.run(_request(), runtime=runtime, run_context=runtime.context)
        return result, send_tool.calls

    result, calls = asyncio.run(run())
    assert calls == []  # send_message 从未执行（零副作用）
    assert result["tool_trace"][0]["status"] == "denied"


def test_unregistered_tool_is_denied():
    """模型请求未注册工具（restart_gateway）：agent 直接拒绝，不执行。"""
    runtime = _runtime()

    async def run():
        agent = _agent(
            {"search_vpn_knowledge": _StubTool("search_vpn_knowledge")},
            _CommandModel(
                ["restart_gateway"], final_command={"command": "ask_customer", "confidence": 0.9}
            ),
        )
        return await agent.run(_request(), runtime=runtime, run_context=runtime.context)

    result = asyncio.run(run())
    assert result["tool_trace"][0]["tool"] == "restart_gateway"
    assert result["tool_trace"][0]["status"] == "denied"
    assert result["tool_trace"][0]["reason"] == "unregistered_tool"


# ========== 验收：工具调用全部过治理（审计/指标） ==========


def test_tool_calls_go_through_governance_and_are_audited():
    tool = _StubTool("search_vpn_knowledge")
    runtime = _runtime()

    async def run():
        agent = _agent(
            {"search_vpn_knowledge": tool},
            _CommandModel(["search_vpn_knowledge"]),
        )
        result = await agent.run(_request(), runtime=runtime, run_context=runtime.context)
        return result, runtime

    result, runtime = asyncio.run(run())
    types = [e[1] for e in runtime.audit.events]
    assert "tool_call_started" in types
    assert "tool_call_completed" in types
    # 可观测性：VPN 工具调用指标
    assert "vpn_tool_calls_total" in runtime.metrics.counts
