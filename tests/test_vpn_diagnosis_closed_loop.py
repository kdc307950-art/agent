"""VPN 客户处置闭环（阶段二）单元与 API 测试。

覆盖：
    - 领域对象校验：VpnDiagnosisRun / VpnCustomerAction / VpnCustomerActionResult /
      VpnEscalation 的 extra="forbid" 与必填字段/置信度范围；
    - provide_steps 草稿 -> VpnCustomerAction 列表解析；
    - 状态机迁移：diagnosing / awaiting_customer_action / reconciliation_required 增补；
    - VpnClosedLoopService 编排：diagnose 落库 + 派步骤 + 状态联动 / 无命令转人工（escalation +
      reconciliation_required）/ submit_action_result 闭环；
    - HTTP API 基本行为（fake runtime + principal override）：scope 校验、503、404、diagnose
      返回 dispatch、客户回填结果。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.security import Principal, rate_limit_dependency
from backend.vpn.closed_loop import VpnClosedLoopService, VpnCustomerActionError
from backend.vpn.diagnosis import (
    CustomerActionStatus,
    DiagnosisRegistry,
    VpnCustomerAction,
    VpnCustomerActionResult,
    VpnDiagnosisFinding,
    VpnDiagnosisRun,
    VpnEscalation,
    parse_steps_to_actions,
)
from backend.vpn.diagnosis_api import router as vpn_diagnosis_router
from src.my_agent.helpdesk import (
    ActorType,
    InvalidTicketTransition,
    TicketAction,
    TicketCommand,
    TicketStatus,
    transition_ticket,
)


# ===========================================================================
# 领域对象校验
# ===========================================================================


def test_vpn_diagnosis_run_extra_forbid_and_defaults():
    run = VpnDiagnosisRun(
        run_id="diag_1", ticket_id="t-1", tenant_id="tenant-a", fault="connection_failed"
    )
    assert run.status.value == "diagnosing"
    assert run.confidence == 0.0
    assert run.evidence == []
    with pytest.raises(ValidationError):
        VpnDiagnosisRun(run_id="diag_1", ticket_id="t-1", tenant_id="tenant-a", bogus=True)


def test_vpn_diagnosis_run_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        VpnDiagnosisRun(run_id="diag_1", ticket_id="t-1", tenant_id="tenant-a", confidence=1.5)


def test_vpn_customer_action_requires_documented_fields():
    with pytest.raises(ValidationError):
        # 缺少必填字段 title / instruction / expected_result / risk_level / requires_agent
        VpnCustomerAction(action_id="a-1", ticket_id="t-1", tenant_id="tenant-a")
    action = VpnCustomerAction(
        action_id="a-1",
        ticket_id="t-1",
        tenant_id="tenant-a",
        title="检查版本",
        instruction="打开客户端查看版本号",
        expected_result="版本号正确",
        risk_level="low",
        requires_agent=False,
    )
    assert action.status.value == "issued"
    assert action.risk_level.value == "low"


def test_vpn_customer_action_rejects_extra_field():
    with pytest.raises(ValidationError):
        VpnCustomerAction(
            action_id="a-1",
            ticket_id="t-1",
            tenant_id="tenant-a",
            title="版本",
            instruction="打开客户端",
            expected_result="版本正确",
            risk_level="low",
            requires_agent=False,
            hacked=True,
        )


def test_vpn_customer_action_result_and_escalation_extra_forbid():
    result = VpnCustomerActionResult(
        action_id="a-1", ticket_id="t-1", tenant_id="tenant-a", result="已重启，版本 3.4.2"
    )
    assert result.submitted_at is not None
    with pytest.raises(ValidationError):
        VpnCustomerActionResult(
            action_id="a-1", ticket_id="t-1", tenant_id="tenant-a", result="x", bogus=1
        )
    esc = VpnEscalation(escalation_id="esc_1", ticket_id="t-1", tenant_id="tenant-a")
    assert esc.status.value == "open"
    assert esc.target_queue == "team-service-desk"
    with pytest.raises(ValidationError):
        VpnEscalation(escalation_id="esc_1", ticket_id="t-1", tenant_id="tenant-a", evil=True)


# ===========================================================================
# provide_steps 草稿 -> VpnCustomerAction 列表
# ===========================================================================


def test_parse_steps_to_actions_splits_numbered_draft():
    actions = parse_steps_to_actions(
        "1. 检查客户端版本\n2. 重启客户端\n3. 切换网络",
        ticket_id="t-1",
        tenant_id="tenant-a",
        run_id="diag_1",
    )
    assert len(actions) == 3
    for idx, action in enumerate(actions):
        assert action.ticket_id == "t-1"
        assert action.tenant_id == "tenant-a"
        assert action.run_id == "diag_1"
        assert action.requires_agent is False
        assert action.risk_level.value == "low"
        assert action.action_id.startswith("act_")
        assert action.order == idx
    assert "客户端版本" in actions[0].instruction
    assert actions[0].expected_result


def test_parse_steps_to_actions_single_line_fallback():
    actions = parse_steps_to_actions("重启 VPN 客户端", ticket_id="t-1", tenant_id="tenant-a")
    assert len(actions) == 1
    assert actions[0].title


def test_parse_steps_to_actions_empty_returns_empty():
    assert parse_steps_to_actions("", ticket_id="t-1", tenant_id="tenant-a") == []


# ===========================================================================
# 状态机迁移（阶段二增补）
# ===========================================================================


def _cmd(action: TicketAction, actor_type: ActorType) -> TicketCommand:
    return TicketCommand(ticket_id="t-1", action=action, actor_type=actor_type, actor_id="x", expected_version=1)


def test_diagnosing_transitions():
    assert (
        transition_ticket(
            TicketStatus.IN_PROGRESS,
            _cmd(TicketAction.START_DIAGNOSIS, ActorType.AGENT),
            scopes={"ticket:agent"},
        )
        == TicketStatus.DIAGNOSING
    )
    assert (
        transition_ticket(
            TicketStatus.DIAGNOSING,
            _cmd(TicketAction.PRESCRIBE_STEPS, ActorType.AGENT),
            scopes={"ticket:agent"},
        )
        == TicketStatus.AWAITING_CUSTOMER_ACTION
    )
    assert (
        transition_ticket(
            TicketStatus.AWAITING_CUSTOMER_ACTION,
            _cmd(TicketAction.PROVIDE_ACTION_RESULT, ActorType.CUSTOMER),
            scopes={"ticket:customer"},
        )
        == TicketStatus.DIAGNOSING
    )
    assert (
        transition_ticket(
            TicketStatus.DIAGNOSING,
            _cmd(TicketAction.REQUEST_RECONCILIATION, ActorType.AGENT),
            scopes={"ticket:agent"},
        )
        == TicketStatus.RECONCILIATION_REQUIRED
    )
    assert (
        transition_ticket(
            TicketStatus.RECONCILIATION_REQUIRED,
            _cmd(TicketAction.RECONCILE, ActorType.AGENT),
            scopes={"ticket:agent"},
        )
        == TicketStatus.IN_PROGRESS
    )


def test_customer_cannot_prescribe_steps_only_customer_can_submit_result():
    with pytest.raises(Exception):
        transition_ticket(
            TicketStatus.DIAGNOSING,
            _cmd(TicketAction.PRESCRIBE_STEPS, ActorType.CUSTOMER),
            scopes={"ticket:customer"},
        )
    # 客户可回填动作结果
    assert (
        transition_ticket(
            TicketStatus.AWAITING_CUSTOMER_ACTION,
            _cmd(TicketAction.PROVIDE_ACTION_RESULT, ActorType.CUSTOMER),
            scopes={"ticket:customer"},
        )
        == TicketStatus.DIAGNOSING
    )
    # 客户不能回填（无权执行 PRESCRIBE_STEPS）
    with pytest.raises(Exception):
        transition_ticket(
            TicketStatus.DIAGNOSING,
            _cmd(TicketAction.PRESCRIBE_STEPS, ActorType.CUSTOMER),
            scopes={"ticket:customer"},
        )


# ===========================================================================
# VpnClosedLoopService 编排（fake runtime + stub diagnosis_service）
# ===========================================================================


class _FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def record_event(self, context, event_type, status=None, payload=None):
        self.events.append((event_type, status or "", payload or {}))


class _FakeTickets:
    def __init__(self, *, initial_status=TicketStatus.IN_PROGRESS, version=0, requester_id="user-042"):
        self.cur_status = initial_status
        self.version = version
        self.requester_id = requester_id
        self.transition_calls: list[TicketAction] = []

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
        return SimpleNamespace(ticket_id=command.ticket_id, status=self.cur_status, version=self.version)


class _StubDiagnosisService:
    """返回预置 outcome 的诊断服务桩（避免真实 LLM/工具调用）。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    async def run_with_context(self, *, runtime, tenant_id, ticket_id, run_context):
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0]


def _provide_steps_outcome():
    return {
        "request": {"fault": "connection_failed", "ticket_id": "t-1", "tenant_id": "tenant-a"},
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


def _handoff_outcome():
    return {
        "request": {"fault": "multi_user_impact", "ticket_id": "t-1", "tenant_id": "tenant-a"},
        "result": {
            "command": None,
            "must_handoff": True,
            "evaluation": {
                "must_handoff": True,
                "handoff_reasons": ["multi_user_impact"],
                "confidence": 0.5,
            },
            "tool_evidence": [],
        },
    }


def _make_loop(initial_status=TicketStatus.IN_PROGRESS, outcomes=None, requester_id="user-042"):
    tickets = _FakeTickets(initial_status=initial_status, requester_id=requester_id)
    audit = _FakeAudit()
    svc = VpnClosedLoopService(
        diagnosis_service=_StubDiagnosisService(outcomes or [_provide_steps_outcome()]),
        registry=DiagnosisRegistry(),
    )
    runtime = SimpleNamespace(tickets=tickets, audit=audit, vpn_closed_loop=svc)
    return runtime, svc, tickets, audit


def _rc(scope="ticket:agent", user_id="user-1"):
    return SimpleNamespace(
        tenant_id="tenant-a",
        user_id=user_id,
        scopes=frozenset({scope}),
    )


def test_diagnose_persists_run_and_prescribes_steps():
    runtime, svc, tickets, audit = _make_loop()
    out = asyncio.run(svc.diagnose(runtime=runtime, tenant_id="tenant-a", ticket_id="t-1", run_context=_rc()))
    run = out["run"]
    assert run["next_action"] == "provide_steps"
    assert run["ticket_id"] == "t-1"
    assert run["fault"] == "connection_failed"
    assert len(run["evidence"]) == 1
    dispatch = out["dispatch"]
    assert dispatch["status"] == "awaiting_customer_action"
    assert len(dispatch["actions"]) == 3
    # 工单已进入 awaiting_customer_action
    assert tickets.cur_status == TicketStatus.AWAITING_CUSTOMER_ACTION
    assert TicketAction.START_DIAGNOSIS in tickets.transition_calls
    assert TicketAction.PRESCRIBE_STEPS in tickets.transition_calls
    names = [e[0] for e in audit.events]
    assert "vpn_diagnosis_started" in names
    assert "vpn_diagnosis_prescribed_steps" in names


def test_diagnose_must_handoff_records_escalation_and_reconcile():
    runtime, svc, tickets, audit = _make_loop(outcomes=[_handoff_outcome()])
    out = asyncio.run(svc.diagnose(runtime=runtime, tenant_id="tenant-a", ticket_id="t-1", run_context=_rc()))
    assert out["run"]["status"] == "handed_off"
    assert out["dispatch"]["status"] == "handed_off"
    snapshot = asyncio.run(svc.get_snapshot(tenant_id="tenant-a", ticket_id="t-1"))
    assert len(snapshot["escalations"]) == 1
    assert snapshot["escalations"][0]["reason_codes"] == ["multi_user_impact"]
    # 无命令转人工 -> 请求对账
    assert tickets.cur_status == TicketStatus.RECONCILIATION_REQUIRED
    assert TicketAction.REQUEST_RECONCILIATION in tickets.transition_calls


def test_submit_action_result_closes_loop():
    runtime, svc, tickets, audit = _make_loop()
    # 先诊断产出步骤
    asyncio.run(svc.diagnose(runtime=runtime, tenant_id="tenant-a", ticket_id="t-1", run_context=_rc()))
    action = svc.registry.list_actions("t-1")[0]
    # 客户回填结果（用客户 scope）
    out = asyncio.run(
        svc.submit_action_result(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            action_id=action.action_id,
            result="已重启客户端，版本 3.4.2",
            run_context=_rc(scope="ticket:customer", user_id="user-042"),
        )
    )
    assert out["action_result"]["action_id"] == action.action_id
    assert out["transition"] is True
    snapshot = asyncio.run(svc.get_snapshot(tenant_id="tenant-a", ticket_id="t-1"))
    results = snapshot["results"]
    assert len(results) == 1
    assert results[0]["result"] == "已重启客户端，版本 3.4.2"
    # 动作状态已置 EXECUTED
    updated = svc.registry.get_action(action.action_id)
    assert updated.status == CustomerActionStatus.EXECUTED


def test_submit_action_result_rejects_unknown_or_wrong_ticket_action():
    runtime, svc, _tickets, _audit = _make_loop()
    asyncio.run(svc.diagnose(runtime=runtime, tenant_id="tenant-a", ticket_id="t-1", run_context=_rc()))
    with pytest.raises(VpnCustomerActionError):
        asyncio.run(
            svc.submit_action_result(
                runtime=runtime,
                tenant_id="tenant-a",
                ticket_id="t-1",
                action_id="act_unknown",
                result="x",
                run_context=_rc(scope="ticket:customer"),
            )
        )


# ===========================================================================
# HTTP API 基本行为（fake runtime + principal override）
# ===========================================================================


def _make_app(runtime):
    app = FastAPI()
    app.include_router(vpn_diagnosis_router)
    app.state.runtime = runtime
    return app


def _install_principal(app: FastAPI, principal: Principal):
    def _dep():
        return principal

    app.dependency_overrides[rate_limit_dependency] = _dep


def _principal(user_id, scopes, tenant_id="tenant-a") -> Principal:
    return Principal(tenant_id=tenant_id, user_id=user_id, scopes=frozenset(scopes))


def test_diagnose_api_requires_agent_scope():
    runtime, _svc, _tickets, _audit = _make_loop()
    app = _make_app(runtime)
    _install_principal(app, _principal("user-1", ("chat:read",)))
    with TestClient(app) as client:
        resp = client.post("/tickets/t-1/vpn/diagnose")
    assert resp.status_code == 403


def test_diagnose_api_returns_201_and_dispatch():
    runtime, _svc, tickets, _audit = _make_loop()
    app = _make_app(runtime)
    _install_principal(app, _principal("user-1", ("ticket:agent",)))
    with TestClient(app) as client:
        resp = client.post("/tickets/t-1/vpn/diagnose")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["run"]["next_action"] == "provide_steps"
    assert body["dispatch"]["status"] == "awaiting_customer_action"
    assert tickets.cur_status == TicketStatus.AWAITING_CUSTOMER_ACTION


def test_diagnose_api_returns_404_for_missing_ticket():
    # 用一个永远返回 None 的 tickets
    class _NoTicket:
        async def get(self, tenant_id, ticket_id):
            return None

    runtime = SimpleNamespace(tickets=_NoTicket(), audit=_FakeAudit(), vpn_closed_loop=object())
    app = _make_app(runtime)
    _install_principal(app, _principal("user-1", ("ticket:agent",)))
    with TestClient(app) as client:
        resp = client.post("/tickets/t-1/vpn/diagnose")
    assert resp.status_code == 404


def test_diagnose_api_returns_503_when_service_uninitialized():
    runtime = SimpleNamespace(tickets=_FakeTickets(), audit=_FakeAudit(), vpn_closed_loop=None)
    app = _make_app(runtime)
    _install_principal(app, _principal("user-1", ("ticket:agent",)))
    with TestClient(app) as client:
        resp = client.post("/tickets/t-1/vpn/diagnose")
    assert resp.status_code == 503


def test_get_diagnosis_requires_read_scope_and_returns_snapshot():
    runtime, _svc, _tickets, _audit = _make_loop()
    app = _make_app(runtime)
    _install_principal(app, _principal("user-1", ("ticket:agent",)))
    with TestClient(app) as client:
        client.post("/tickets/t-1/vpn/diagnose")
        resp = client.get("/tickets/t-1/vpn/diagnosis")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket_id"] == "t-1"
    assert len(body["actions"]) == 3
    assert body["latest_run"]["next_action"] == "provide_steps"


def test_submit_action_result_requires_customer_and_own_ticket():
    runtime, _svc, tickets, _audit = _make_loop()
    app = _make_app(runtime)
    _install_principal(app, _principal("user-1", ("ticket:agent",)))
    with TestClient(app) as client:
        client.post("/tickets/t-1/vpn/diagnose")
        action_id = client.get("/tickets/t-1/vpn/diagnosis").json()["actions"][0]["action_id"]
        # agent 无权提交客户动作结果
        resp = client.post(f"/tickets/t-1/vpn/actions/{action_id}/result", json={"result": "已处理"})
    assert resp.status_code == 403
    # 客户（本人工单）可提交
    _install_principal(app, _principal("user-042", ("ticket:customer",)))
    with TestClient(app) as client:
        resp2 = client.post(f"/tickets/t-1/vpn/actions/{action_id}/result", json={"result": "已处理"})
    assert resp2.status_code == 200, resp2.text


def test_resume_diagnosis_requires_agent_scope():
    runtime, _svc, _tickets, _audit = _make_loop()
    app = _make_app(runtime)
    _install_principal(app, _principal("user-1", ("chat:read",)))
    with TestClient(app) as client:
        resp = client.post("/tickets/t-1/vpn/diagnose/resume", json={"comment": "r"})
    assert resp.status_code == 403
