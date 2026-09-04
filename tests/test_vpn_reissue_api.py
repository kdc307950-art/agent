"""重新下发 VPN 配置（reissue_vpn_config）HTTP API 端到端集成测试。

证明「可运行性」：经 /vpn/reissue 触发 → 审批 → 受控执行 → 结果，且关联工单/用户/审批人/工具/结果。
不依赖真实 DB（mini-app + 注入 fake runtime + principal override）。

覆盖：
    - 触发（ticket:agent）→ 201 pending + audit vpn_reissue_started + 工单迁 AWAITING_APPROVAL；
    - 审批（ticket:approve）→ 200 delivered/confirmed + audit approved/executed + domain APPROVE 迁移；
    - 重复审批同键 → 不重复执行（幂等）；
    - 缺 ticket:approve → 403；缺 ticket:agent 触发 → 403；
    - 幂等键不匹配 → 409；
    - 查询 → 可见状态。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.security import Principal, rate_limit_dependency
from backend.vpn.api import router as vpn_reissue_router
from backend.vpn.approval import ReissueRegistry
from backend.vpn.mock_adapter import MockVpnAdapter
from backend.vpn.reissue_service import VpnReissueService
from src.my_agent.helpdesk import TicketAction, TicketStatus, transition_ticket


class _FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def record_event(self, context, event_type, status=None, payload=None):
        self.events.append((event_type, status or "", payload or {}))


class _FakeAssets:
    def __init__(self):
        pass

    async def get(self, tenant_id, asset_id):
        return SimpleNamespace(asset_id=asset_id, owner_user_id="user-042", status="active", is_deleted=False)


class _FakeTickets:
    def __init__(self, *, initial_status=TicketStatus.IN_PROGRESS, version=0):
        self.cur_status = initial_status
        self.version = version
        self.transition_calls: list[TicketAction] = []
        self.operation_started: list[str] = []
        self.operation_failed: list[tuple[str, str]] = []
        self.operation_committed: list[str] = []

    async def get(self, tenant_id, ticket_id):
        return SimpleNamespace(ticket_id=ticket_id, status=self.cur_status, version=self.version)

    async def transition(self, tenant_id, command, scopes=None):
        self.transition_calls.append(command.action)
        self.cur_status = transition_ticket(self.cur_status, command, scopes=set(scopes or ()))
        return SimpleNamespace(ticket_id=command.ticket_id, status=self.cur_status, version=self.version)

    async def start_workflow_operation(self, *, tenant_id, ticket_id, operation_id, command_type, expected_version, checkpoint_thread_id):
        self.operation_started.append(operation_id)
        return {"status": "started", "operation_id": operation_id}

    async def mark_workflow_operation_failed(self, *, tenant_id, ticket_id, operation_id, error_code):
        self.operation_failed.append((operation_id, error_code))
        return True

    async def mark_workflow_operation_committed(self, *, tenant_id, ticket_id, operation_id, result_hash):
        self.operation_committed.append(operation_id)
        return True


def _make_app(*, tickets: _FakeTickets, audit: _FakeAudit):
    svc = VpnReissueService(registry=ReissueRegistry())
    runtime = SimpleNamespace(
        vpn_reissue=svc,
        tickets=tickets,
        audit=audit,
        vpn_adapter=MockVpnAdapter(),
        assets=_FakeAssets(),
    )
    app = FastAPI()
    app.include_router(vpn_reissue_router)
    app.state.runtime = runtime
    return app, svc, tickets


def _install_principal(app: FastAPI, principal: Principal):
    def _dep():
        return principal

    app.dependency_overrides[rate_limit_dependency] = _dep


def _principal(user_id: str, scopes: tuple[str, ...], tenant_id="tenant-a") -> Principal:
    return Principal(tenant_id=tenant_id, user_id=user_id, scopes=frozenset(scopes))


def _payload(**overrides) -> dict[str, Any]:
    base = {
        "action": "reissue_vpn_config",
        "user_id": "user-042",
        "asset_id": "asset-001",
        "ticket_id": "t-1",
        "client_version": "v2.5.0",
        "reason_codes": ["gate_passed"],
    }
    base.update(overrides)
    return base


def _event_names(audit: _FakeAudit) -> list[str]:
    return [e[0] for e in audit.events]


def test_start_requires_ticket_agent_scope():
    app, _svc, _tickets = _make_app(tickets=_FakeTickets(), audit=_FakeAudit())
    _install_principal(app, _principal("user-1", ("chat:read",)))
    with TestClient(app) as client:
        response = client.post("/vpn/reissue", json=_payload())
    assert response.status_code == 403


def test_start_then_approve_runs_end_to_end():
    """触发 → 审批 → 受控执行成功，delivered/confirmed，且关联工单/审批人/工具/结果。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    approver = _principal("approver-1", ("ticket:approve",))

    with TestClient(app) as client:
        # 触发
        _install_principal(app, agent)
        resp = client.post("/vpn/reissue", json=_payload())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        key = body["idempotency_key"]
        assert TicketAction.REQUEST_APPROVAL in tickets.transition_calls
        assert tickets.cur_status == TicketStatus.AWAITING_APPROVAL
        # 审批
        _install_principal(app, approver)
        resp2 = client.post(f"/vpn/reissue/{key}/approve", json=_payload())
        assert resp2.status_code == 200, resp2.text
        result = resp2.json()
        assert result["ok"] is True
        assert result["delivered"] is True
        assert result["confirmed"] is True
        assert result["status"] == "confirmed"
        # 域名 APPROVE 迁移
        assert TicketAction.APPROVE in tickets.transition_calls
        # 审计关联
        names = _event_names(audit)
        for expected in ("vpn_reissue_started", "vpn_reissue_approved", "vpn_reissue_executed"):
            assert expected in names, f"缺审计事件 {expected}"
        approved_payload = next(p for (e, s, p) in audit.events if e == "vpn_reissue_approved")
        assert approved_payload["approver_user_id"] == "approver-1"
        assert approved_payload["ticket_id"] == "t-1"
        assert approved_payload["action"] == "reissue_vpn_config"
        assert key in tickets.operation_committed  # C1 committed 终态
        # 查询
        _install_principal(app, agent)
        resp3 = client.get(f"/vpn/reissue/{key}")
        assert resp3.status_code == 200
        assert resp3.json()["status"] == "confirmed"


def test_repeated_approve_does_not_repeat_execution():
    """同一幂等键重复审批 → 只执行一次（幂等），审计 executed 仅一次。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    approver = _principal("approver-1", ("ticket:approve",))

    with TestClient(app) as client:
        _install_principal(app, agent)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        _install_principal(app, approver)
        client.post(f"/vpn/reissue/{key}/approve", json=_payload())
        second = client.post(f"/vpn/reissue/{key}/approve", json=_payload())
        assert second.status_code == 200
    executed_events = [e for e in audit.events if e[0] == "vpn_reissue_executed"]
    assert len(executed_events) == 1  # 重复审批不重复执行


def test_approve_requires_approve_scope_and_key_match():
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    with TestClient(app) as client:
        _install_principal(app, agent)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        # 缺 ticket:approve
        _install_principal(app, agent)
        resp = client.post(f"/vpn/reissue/{key}/approve", json=_payload())
        assert resp.status_code == 403
        # 幂等键不匹配
        _install_principal(app, _principal("approver-1", ("ticket:approve",)))
        resp2 = client.post("/vpn/reissue/wrong-key/approve", json=_payload())
        assert resp2.status_code == 409
