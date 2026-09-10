"""受理图侧 VPN 闭环集成测试：graph.vpn_diagnose_node 接入 VpnClosedLoopService（阶段二验收点）。

覆盖：
    - it.vpn + 字段齐 + runtime 注入 vpn_closed_loop -> 触发闭环（diagnose 落 run）-> 状态机
      START_DIAGNOSIS/PRESCRIBE_STEPS -> awaiting_customer_action，vpn_diagnosis_* 回填；
    - 非 it.vpn 透传（vpn_diagnosis_run=False，闭环不触发）；
    - 字段不齐透传（vpn_diagnosis_run=False）；
    - 无 vpn_closed_loop 服务 -> 回退既有的只读 vpn_diagnosis 路径（不抛异常）。

采用 fake runtime（注入 stub vpn_closed_loop + audit + tickets transition），单测走内存
DiagnosisRegistry（repository=None），与 test_vpn_diagnosis_closed_loop.py 口径一致。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver

from backend.vpn.closed_loop import VpnClosedLoopService
from backend.vpn.diagnosis import DiagnosisRegistry
from src.my_agent.helpdesk import (
    ClassificationResult,
    TicketAction,
    TicketCategory,
    TicketStatus,
    build_helpdesk_intake_graph,
    transition_ticket,
)


class _FixedClassifier:
    """固定分类器：返回预设的 category/subcategory。"""

    def __init__(self, category: TicketCategory, *, subcategory: str = "general"):
        self.result = ClassificationResult(
            category=category, subcategory=subcategory, signals=("test",), confidence=0.9
        )

    async def classify(self, text, fields):
        return self.result


def _config():
    return {
        "configurable": {
            "thread_id": f"vpn-intake-{uuid4().hex}",
            "tenant_id": "tenant-a",
            "user_id": "customer-1",
        }
    }


def _invoke(graph, inputs, run_config):
    return asyncio.run(graph.ainvoke(inputs, run_config))


def _base(**overrides):
    state: dict[str, Any] = {
        "ticket_id": "ticket-1",
        "requester_id": "customer-1",
        "text": "VPN 无法连接，客户端一直转圈",
        "fields": {
            "title": "VPN 无法连接",
            "description": "客户端一直转圈，提示错误码 809",
            "affected_system": "VPN",
            "impact": "one user",
        },
        "clarification_rounds": 0,
    }
    state.update(overrides)
    return state


class _FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def record_event(self, context, event_type, status=None, payload=None):
        self.events.append((event_type, status or "", payload or {}))


class _FakeTickets:
    def __init__(
        self, *, initial_status=TicketStatus.IN_PROGRESS, version=0, requester_id="customer-1"
    ):
        self.cur_status = initial_status
        self.version = version
        self.requester_id = requester_id
        self.transition_calls: list[TicketAction] = []
        self.timeline: list[str] = []

    async def get(self, tenant_id, ticket_id):
        return SimpleNamespace(
            ticket_id=ticket_id,
            status=self.cur_status,
            version=self.version,
            requester_id=self.requester_id,
        )

    async def transition(self, tenant_id, command, scopes=None):
        self.transition_calls.append(command.action)
        self.cur_status = transition_ticket(self.cur_status, command, scopes=set(scopes or ()))
        return SimpleNamespace(
            ticket_id=command.ticket_id, status=self.cur_status, version=self.version
        )

    async def append_status_event(
        self, tenant_id, ticket_id, *, action, actor_type, actor_id, payload=None
    ):
        self.timeline.append(action)
        return True


class _StubDiagnosisService:
    """提供 provide_steps 结果的诊断服务桩（避免真实 LLM/工具调用）。"""

    def __init__(self, outcome=None):
        self.outcome = outcome or _provide_steps_outcome()
        self.calls = 0

    async def run_with_context(self, *, runtime, tenant_id, ticket_id, run_context):
        self.calls += 1
        return self.outcome


def _provide_steps_outcome():
    return {
        "request": {"fault": "connection_failed", "ticket_id": "ticket-1", "tenant_id": "tenant-a"},
        "result": {
            "command": {
                "command": "provide_steps",
                "content": "1. 检查客户端版本\n2. 重启客户端\n3. 切换网络",
                "payload": {},
                "reason_codes": ["fault_connection"],
                "confidence": 0.91,
            },
            "must_handoff": False,
            "evaluation": {"must_handoff": False, "handoff_reasons": [], "confidence": 0.91},
            "tool_evidence": [
                {"tool_name": "get_vpn_account_status", "content": "账号正常", "found": True}
            ],
        },
    }


class _ReadOnlyVpn:
    """只读诊断桩：用于「无 vpn_closed_loop 时回退只读路径」的用例。"""

    def __init__(self):
        self.calls = 0
        self.last_request = None

    async def diagnose(self, request, runtime, run_context):
        self.calls += 1
        self.last_request = request
        return {"raw": True}

    def apply_handoff(self, raw, request):
        return {
            "must_handoff": False,
            "command": {
                "command": "ask_customer",
                "content": "请补充客户端版本",
                "payload": {},
                "reason_codes": ["gate_passed"],
                "confidence": 0.9,
            },
            "evaluation": {"must_handoff": False, "handoff_reasons": [], "confidence": 0.9},
            "error_code": None,
        }


def _make_closed_loop_runtime():
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS, requester_id="customer-1")
    closed_loop = VpnClosedLoopService(
        diagnosis_service=_StubDiagnosisService(),
        registry=DiagnosisRegistry(),
        repository=None,
    )
    runtime = SimpleNamespace(
        vpn_closed_loop=closed_loop,
        tickets=tickets,
        audit=audit,
    )
    return runtime, closed_loop, tickets, audit


# ========== it.vpn + 字段齐 -> 触发闭环 -> 状态机联动 ==========


def test_vpn_complete_flow_triggers_closed_loop_and_state_machine():
    runtime, closed_loop, tickets, audit = _make_closed_loop_runtime()
    graph = build_helpdesk_intake_graph(
        classifier=_FixedClassifier(TicketCategory.IT, subcategory="vpn"),
        checkpointer=MemorySaver(),
        vpn_diagnosis=_StubDiagnosisService(),
        runtime_provider=lambda: runtime,
    )

    result = _invoke(graph, _base(), _config())

    # 闭环被触发并落 run / next_action / must_handoff
    assert result["vpn_diagnosis_run"] is True
    assert result["vpn_diagnosis_run_id"]
    assert result["vpn_diagnosis_next_action"] == "provide_steps"
    assert result["vpn_diagnosis_must_handoff"] is False
    # 状态机联动：IN_PROGRESS -> diagnosing -> awaiting_customer_action
    assert tickets.cur_status == TicketStatus.AWAITING_CUSTOMER_ACTION
    assert TicketAction.START_DIAGNOSIS in tickets.transition_calls
    assert TicketAction.PRESCRIBE_STEPS in tickets.transition_calls
    # 排查步骤已持久化（内存 registry）
    actions = closed_loop.registry.list_actions("ticket-1")
    assert len(actions) == 3
    assert actions[0].action_id
    # 时间线进入（诊断/派步骤）
    assert "vpn_diagnosis" in tickets.timeline
    assert "vpn_prescribed_steps" in tickets.timeline
    # 审计
    assert any(e[0] == "vpn_diagnosis_started" for e in audit.events)


# ========== 非 it.vpn 透传 ==========


def test_non_vpn_category_is_lazy_passthrough():
    runtime, closed_loop, tickets, _audit = _make_closed_loop_runtime()
    graph = build_helpdesk_intake_graph(
        classifier=_FixedClassifier(TicketCategory.IT, subcategory="account"),
        checkpointer=MemorySaver(),
        vpn_diagnosis=_StubDiagnosisService(),
        runtime_provider=lambda: runtime,
    )

    result = _invoke(graph, _base(), _config())

    assert result["vpn_diagnosis_run"] is False
    # 闭环未触发
    assert closed_loop.registry.list_runs("ticket-1") == []
    assert tickets.cur_status == TicketStatus.IN_PROGRESS


# ========== 字段不齐透传 ==========


def test_missing_fields_passed_through_without_closed_loop():
    runtime, closed_loop, tickets, _audit = _make_closed_loop_runtime()
    graph = build_helpdesk_intake_graph(
        classifier=_FixedClassifier(TicketCategory.IT, subcategory="vpn"),
        checkpointer=MemorySaver(),
        vpn_diagnosis=_StubDiagnosisService(),
        runtime_provider=lambda: runtime,
    )
    # 缺 affected_system/impact 且追问轮数达到上限 -> 直接派单，vpn_diagnose 命中 missing_fields
    state = _base(fields={"title": "VPN", "description": "无法连接"}, clarification_rounds=3)

    result = _invoke(graph, state, _config())

    assert result["vpn_diagnosis_run"] is False
    assert closed_loop.registry.list_runs("ticket-1") == []
    assert tickets.cur_status == TicketStatus.IN_PROGRESS


# ========== 无 vpn_closed_loop 服务 -> 回退只读诊断 ==========


def test_no_closed_loop_falls_back_to_read_only_diagnosis():
    # runtime 不注入 vpn_closed_loop，只读 vpn_diagnosis 作为兜底
    read_only = _ReadOnlyVpn()
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS, requester_id="customer-1")
    runtime = SimpleNamespace(vpn_closed_loop=None, tickets=tickets, audit=audit)
    graph = build_helpdesk_intake_graph(
        classifier=_FixedClassifier(TicketCategory.IT, subcategory="vpn"),
        checkpointer=MemorySaver(),
        vpn_diagnosis=read_only,
        runtime_provider=lambda: runtime,
    )

    result = _invoke(graph, _base(), _config())

    # 回退到只读诊断路径：诊断被调用，tickets 不做状态机迁移（只读桥接）
    assert read_only.calls == 1
    assert result["vpn_diagnosis_run"] is True
    assert result["vpn_diagnosis_command"]["command"] == "ask_customer"
    assert tickets.cur_status == TicketStatus.IN_PROGRESS
    assert tickets.transition_calls == []
