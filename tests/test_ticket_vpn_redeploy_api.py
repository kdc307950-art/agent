"""Tests for the ticket-to-tenant-redeploy bridge.

The route may create only an approval request. FMG install remains reachable
solely from the existing approve route after a fresh preview hash check.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.security import Principal, rate_limit_dependency
from backend.ticket_api import router as ticket_router
from backend.vpn.api import router as vpn_router
from backend.vpn.approval import ReissueRegistry
from backend.vpn.reissue_service import VpnReissueService
from src.my_agent.helpdesk import TicketAction, TicketStatus, transition_ticket


class _Audit:
    async def record_event(self, *args, **kwargs) -> None:
        del args, kwargs


class _Gateway:
    def __init__(self) -> None:
        self.preview_calls = 0
        self.install_calls = 0

    async def validate_tenant_target(self, *, tenant_id: str) -> dict:
        return {"ok": True, "tenant_id": tenant_id, "target": {"adom": "ADOM_DEMO"}}

    async def preview_tenant_config(self, *, tenant_id: str) -> dict:
        self.preview_calls += 1
        return {
            "ok": True,
            "status": "previewed",
            "tenant_id": tenant_id,
            "target": {"adom": "ADOM_DEMO", "device": "FGT-FAKE-001"},
            "diff_snapshot": {"changes": ["vpn-package"]},
            "diff_hash": "approved-preview-hash",
        }


class _Tickets:
    def __init__(self, records: dict[tuple[str, str], SimpleNamespace]) -> None:
        self.records = records
        self.transition_calls: list[TicketAction] = []

    async def get(self, tenant_id: str, ticket_id: str):
        return self.records.get((tenant_id, ticket_id))

    async def transition(self, tenant_id: str, command, scopes=None):
        record = self.records[(tenant_id, command.ticket_id)]
        record.status = transition_ticket(record.status, command, scopes=set(scopes or ()))
        self.transition_calls.append(command.action)
        return record

    async def start_workflow_operation(self, **kwargs):
        del kwargs
        return {"status": "started", "intent": None}


def _principal(*, tenant_id: str = "demo", scopes: tuple[str, ...] = ("ticket:agent",)) -> Principal:
    return Principal(tenant_id=tenant_id, user_id="agent-1", scopes=frozenset(scopes))


def _make_app(*, ticket: SimpleNamespace | None, tenant_id: str = "demo"):
    records = {} if ticket is None else {(tenant_id, ticket.ticket_id): ticket}
    tickets = _Tickets(records)
    gateway = _Gateway()
    runtime = SimpleNamespace(
        tickets=tickets,
        audit=_Audit(),
        vpn_command_gateway=gateway,
        vpn_reissue=VpnReissueService(registry=ReissueRegistry()),
    )
    app = FastAPI()
    app.include_router(ticket_router)
    app.include_router(vpn_router)
    app.state.runtime = runtime
    return app, tickets, gateway


def _ticket(*, category: str | None = "it.vpn", status: TicketStatus = TicketStatus.IN_PROGRESS):
    return SimpleNamespace(
        ticket_id="vpn-100",
        category=category,
        status=status,
        version=7,
        requester_id="customer-1",
    )


def _install_principal(app: FastAPI, principal: Principal) -> None:
    app.dependency_overrides[rate_limit_dependency] = lambda: principal


def test_vpn_ticket_creates_previewed_approval_without_install() -> None:
    app, tickets, gateway = _make_app(ticket=_ticket())
    _install_principal(app, _principal())

    with TestClient(app) as client:
        response = client.post(
            "/tickets/vpn-100/vpn/redeploy-request",
            json={"reason_codes": ["client_config_drift"]},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["idempotency_key"] == "redeploy:demo:vpn-100"
    assert body["request"]["action"] == "redeploy_tenant_vpn_config"
    assert body["request"]["user_id"] is None
    assert gateway.preview_calls == 1
    assert gateway.install_calls == 0
    assert tickets.transition_calls == [TicketAction.REQUEST_APPROVAL]
    assert _ticket_status(tickets, "demo", "vpn-100") == TicketStatus.AWAITING_APPROVAL


def test_vpn_ticket_bridge_rejects_wrong_scope_category_and_state() -> None:
    app, _tickets, _gateway = _make_app(ticket=_ticket())
    _install_principal(app, _principal(scopes=("ticket:customer",)))
    with TestClient(app) as client:
        assert client.post("/tickets/vpn-100/vpn/redeploy-request", json={}).status_code == 403

    app, _tickets, _gateway = _make_app(ticket=_ticket(category="it.network"))
    _install_principal(app, _principal())
    with TestClient(app) as client:
        assert client.post("/tickets/vpn-100/vpn/redeploy-request", json={}).status_code == 409

    app, _tickets, _gateway = _make_app(ticket=_ticket(status=TicketStatus.ASSIGNED))
    _install_principal(app, _principal())
    with TestClient(app) as client:
        assert client.post("/tickets/vpn-100/vpn/redeploy-request", json={}).status_code == 409


def test_vpn_ticket_bridge_hides_other_tenant_ticket() -> None:
    app, _tickets, _gateway = _make_app(ticket=_ticket(), tenant_id="tenant-a")
    _install_principal(app, _principal(tenant_id="tenant-b"))
    with TestClient(app) as client:
        response = client.post("/tickets/vpn-100/vpn/redeploy-request", json={})
    assert response.status_code == 404


def _ticket_status(tickets: _Tickets, tenant_id: str, ticket_id: str) -> TicketStatus:
    return tickets.records[(tenant_id, ticket_id)].status
