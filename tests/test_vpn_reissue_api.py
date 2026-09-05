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
from backend.vpn.approval import ApprovalStatus, ReissueRegistry
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
    """触发（start）路由的请求体；approve/reject 不再接受该全量快照。"""
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


def _approval_body(operation_id: str, decision: str = "approve") -> dict[str, Any]:
    """approve/reject 路由的最小请求体（仅 operation_id + decision）。"""
    return {"operation_id": operation_id, "decision": decision}


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
        # 审批（最小请求体：仅 operation_id + decision）
        _install_principal(app, approver)
        resp2 = client.post(f"/vpn/reissue/{key}/approve", json=_approval_body(key))
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
        # 查询（经 store 读，返回 operation_id 而非 idempotency_key）
        _install_principal(app, agent)
        resp3 = client.get(f"/vpn/reissue/{key}")
        assert resp3.status_code == 200
        body3 = resp3.json()
        assert body3["operation_id"] == key
        assert body3["status"] == "confirmed"
        assert body3["result"]["ok"] is True


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
        client.post(f"/vpn/reissue/{key}/approve", json=_approval_body(key))
        second = client.post(f"/vpn/reissue/{key}/approve", json=_approval_body(key))
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
        resp = client.post(f"/vpn/reissue/{key}/approve", json=_approval_body(key))
        assert resp.status_code == 403
        # 幂等键不匹配（body operation_id != path operation_id）
        _install_principal(app, _principal("approver-1", ("ticket:approve",)))
        resp2 = client.post("/vpn/reissue/wrong-key/approve", json=_approval_body("wrong-key"))
        assert resp2.status_code == 409


# ===========================================================================
# 新增：审批请求体最小化 + store 读 + 跨租户/状态机错误映射（in-memory store）
# ===========================================================================


def test_approve_reject_route_returns_rejected():
    """/reject 路由：approve 后另走拒绝通道 → REJECTED 终态 + 审计 vpn_reissue_rejected。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    approver = _principal("approver-1", ("ticket:approve",))

    with TestClient(app) as client:
        _install_principal(app, agent)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        _install_principal(app, approver)
        resp = client.post(f"/vpn/reissue/{key}/reject", json=_approval_body(key, "reject"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["error_code"] == "rejected"
    assert "vpn_reissue_rejected" in _event_names(audit)


def test_approve_unsupported_decision():
    """/approve 路由 decision != approve → 服务层返回 unsupported_decision（HTTP 400）。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    approver = _principal("approver-1", ("ticket:approve",))

    with TestClient(app) as client:
        _install_principal(app, agent)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        _install_principal(app, approver)
        # decision 为合法字面量但非 "approve"（在 /approve 路由上）→ 服务层 unsupported_decision
        resp = client.post(f"/vpn/reissue/{key}/approve", json=_approval_body(key, "reject"))
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error_code"] == "unsupported_decision"


def test_approve_decision_outside_literal_returns_422():
    """/approve 路由 decision 不是 approve/reject → pydantic Literal 校验 422。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    with TestClient(app) as client:
        _install_principal(app, agent)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        _install_principal(app, _principal("approver-1", ("ticket:approve",)))
        resp = client.post(f"/vpn/reissue/{key}/approve", json={"operation_id": key, "decision": "hold"})
        assert resp.status_code == 422


def test_get_unknown_operation_returns_404():
    """GET 一个不存在的 operation_id（同租户）→ 404。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    with TestClient(app) as client:
        _install_principal(app, agent)
        resp = client.get("/vpn/reissue/reissue:tenant-a:u:a:t:v")
        assert resp.status_code == 404


def test_cross_tenant_approve_returns_403():
    """跨租户审批：operation_id 内嵌租户与主主体不一致 → 403。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent_a = _principal("user-1", ("ticket:agent",), tenant_id="tenant-a")
    approver_b = _principal("approver-1", ("ticket:approve",), tenant_id="tenant-b")

    with TestClient(app) as client:
        _install_principal(app, agent_a)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        # key 内嵌 tenant-a；以 tenant-b 主体审批 → 403
        _install_principal(app, approver_b)
        resp = client.post(f"/vpn/reissue/{key}/approve", json=_approval_body(key))
        assert resp.status_code == 403
        # 跨租户查询 → 403
        resp_get = client.get(f"/vpn/reissue/{key}")
        assert resp_get.status_code == 403
        # 跨租户拒绝 → 403
        resp_rej = client.post(f"/vpn/reissue/{key}/reject", json=_approval_body(key, "reject"))
        assert resp_rej.status_code == 403


def _make_unknown_operation(app: FastAPI, svc: VpnReissueService, client, *, principal: Principal) -> str:
    _install_principal(app, principal)
    key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
    import asyncio

    asyncio.run(
        svc.store.set_status(
            tenant_id=principal.tenant_id,
            operation_id=key,
            from_status=ApprovalStatus.PENDING,
            to_status=ApprovalStatus.EXECUTION_UNKNOWN,
        )
    )
    return key


def test_confirm_submission_submitted_reconciles_without_install_retry():
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, svc, _tickets = _make_app(tickets=tickets, audit=audit)
    principal = _principal("agent-1", ("ticket:agent", "vpn:reconcile"))
    with TestClient(app) as client:
        key = _make_unknown_operation(app, svc, client, principal=principal)
        _install_principal(app, principal)
        response = client.post(
            f"/vpn/reissue/{key}/confirm-submission",
            json={
                "operation_id": key,
                "submission_state": "submitted",
                "vendor_task_id": 123,
                "note": "FMG Task Manager 已查到任务",
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vendor_task_id"] == 123
    assert body["status"] == "confirmed"
    assert "vpn_reissue_submission_confirmed" in _event_names(audit)


def test_confirm_submission_not_submitted_marks_failed():
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, svc, _tickets = _make_app(tickets=tickets, audit=audit)
    principal = _principal("agent-1", ("ticket:agent", "vpn:reconcile"))
    with TestClient(app) as client:
        key = _make_unknown_operation(app, svc, client, principal=principal)
        _install_principal(app, principal)
        response = client.post(
            f"/vpn/reissue/{key}/confirm-submission",
            json={"operation_id": key, "submission_state": "not_submitted"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["submission_state"] == "not_submitted"


def test_confirm_submission_validation_and_cross_tenant_guards():
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, svc, _tickets = _make_app(tickets=tickets, audit=audit)
    owner = _principal("agent-1", ("ticket:agent", "vpn:reconcile"), tenant_id="tenant-a")
    with TestClient(app) as client:
        key = _make_unknown_operation(app, svc, client, principal=owner)
        _install_principal(app, _principal("other", ("ticket:agent", "vpn:reconcile"), tenant_id="tenant-b"))
        cross = client.post(
            f"/vpn/reissue/{key}/confirm-submission",
            json={"operation_id": key, "submission_state": "submitted", "vendor_task_id": 1},
        )
        assert cross.status_code == 403
        _install_principal(app, owner)
        missing_task = client.post(
            f"/vpn/reissue/{key}/confirm-submission",
            json={"operation_id": key, "submission_state": "submitted"},
        )
        assert missing_task.status_code == 422
        extra_task = client.post(
            f"/vpn/reissue/{key}/confirm-submission",
            json={"operation_id": key, "submission_state": "not_submitted", "vendor_task_id": 1},
        )
        assert extra_task.status_code == 422
        mismatch = client.post(
            f"/vpn/reissue/{key}/confirm-submission",
            json={"operation_id": "wrong", "submission_state": "submitted", "vendor_task_id": 1},
        )
        assert mismatch.status_code == 409


def test_confirm_submission_requires_dedicated_scope():
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("agent-1", ("ticket:agent",))
    with TestClient(app) as client:
        key = _make_unknown_operation(app, svc, client, principal=agent)
        _install_principal(app, agent)
        response = client.post(
            f"/vpn/reissue/{key}/confirm-submission",
            json={"operation_id": key, "submission_state": "submitted", "vendor_task_id": 1},
        )
    assert response.status_code == 403


def test_approve_missing_operation_id_returns_422():
    """approve 请求体缺少 operation_id → pydantic 必填校验 422。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    approver = _principal("approver-1", ("ticket:approve",))
    with TestClient(app) as client:
        _install_principal(app, agent)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        _install_principal(app, approver)
        resp = client.post(f"/vpn/reissue/{key}/approve", json={"decision": "approve"})
        assert resp.status_code == 422


def test_approve_extra_fields_forbidden():
    """approve 请求体携带额外字段（完整快照）→ extra=forbid 422。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    approver = _principal("approver-1", ("ticket:approve",))
    with TestClient(app) as client:
        _install_principal(app, agent)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        _install_principal(app, approver)
        resp = client.post(
            f"/vpn/reissue/{key}/approve",
            json={**_approval_body(key), "user_id": "user-042", "tenant_id": "tenant-a"},
        )
        assert resp.status_code == 422


def test_repeated_approve_still_idempotent_executed_once():
    """重复审批同一操作（operation_id + decision）→ 幂等：只执行一次，终态 confirmed。"""
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc, _tickets = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    approver = _principal("approver-1", ("ticket:approve",))

    with TestClient(app) as client:
        _install_principal(app, agent)
        key = client.post("/vpn/reissue", json=_payload()).json()["idempotency_key"]
        _install_principal(app, approver)
        r1 = client.post(f"/vpn/reissue/{key}/approve", json=_approval_body(key))
        r2 = client.post(f"/vpn/reissue/{key}/approve", json=_approval_body(key))
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json()["status"] == "confirmed"  # 二次审批返回既有终态
        # 查询确认终态 + 只执行一次
        _install_principal(app, agent)
        body = client.get(f"/vpn/reissue/{key}").json()
        assert body["status"] == "confirmed"
    executed_events = [e for e in audit.events if e[0] == "vpn_reissue_executed"]
    assert len(executed_events) == 1
