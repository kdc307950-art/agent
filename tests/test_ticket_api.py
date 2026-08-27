import base64
import hashlib
import hmac
import importlib
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import quote

from fastapi.testclient import TestClient
from langgraph.types import Interrupt

from backend.rate_limit import InMemoryRateLimiter
from backend.security import make_tenant_token
from src.my_agent.helpdesk import ActorType, TicketStatus


SECRET = "test-tenant-secret"


class FakeTickets:
    def __init__(self):
        self.items = {}
        self.created = []
        self.transitions = []
        self.inbound = []
        self.list_calls = []
        self.workflow_runs = {}
        self.fail_next_transition = False

    async def create(self, tenant_id, request):
        self.created.append((tenant_id, request))
        record = SimpleNamespace(
            tenant_id=tenant_id,
            ticket_id=request.ticket_id,
            requester_id=request.requester_id,
            status=TicketStatus.NEW,
            version=0,
        )
        self.items[(tenant_id, request.ticket_id)] = record
        return record

    async def get(self, tenant_id, ticket_id):
        return self.items.get((tenant_id, ticket_id))

    async def list_tickets(self, tenant_id, **kwargs):
        self.list_calls.append((tenant_id, kwargs))
        return list(self.items.values())[: kwargs["limit"]]

    async def start_workflow_operation(self, **kwargs):
        key = (kwargs["tenant_id"], kwargs["ticket_id"], kwargs["operation_id"])
        run = self.workflow_runs.setdefault(key, {"status": "started", "intent": None, **kwargs})
        return run

    async def get_workflow_operation(self, *, tenant_id, ticket_id, operation_id):
        return self.workflow_runs.get((tenant_id, ticket_id, operation_id))

    async def record_workflow_intent(self, *, tenant_id, ticket_id, operation_id, intent, checkpoint_id=None):
        run = self.workflow_runs[(tenant_id, ticket_id, operation_id)]
        run["status"] = "intent_recorded"
        run["intent"] = intent

    async def transition(self, tenant_id, command, *, scopes):
        return await self.transition_many(tenant_id, [command], scopes=scopes)

    async def transition_many(self, tenant_id, commands, *, scopes, operation_id=None):
        next_status = {
            "start_intake": TicketStatus.INTAKING,
            "request_information": TicketStatus.AWAITING_CUSTOMER,
            "provide_information": TicketStatus.INTAKING,
            "classify": TicketStatus.CLASSIFIED,
            "queue": TicketStatus.QUEUED,
            "assign": TicketStatus.ASSIGNED,
            "start_work": TicketStatus.IN_PROGRESS,
            "resolve": TicketStatus.RESOLVED,
            "close": TicketStatus.CLOSED,
            "cancel": TicketStatus.CANCELLED,
        }
        if self.fail_next_transition:
            self.fail_next_transition = False
            raise RuntimeError("injected database failure")
        record = self.items[(tenant_id, commands[0].ticket_id)]
        for command in commands:
            self.transitions.append((tenant_id, command, scopes))
            record.status = next_status[command.action.value]
            record.version = command.expected_version + 1
        if operation_id is not None:
            self.workflow_runs[(tenant_id, record.ticket_id, operation_id)]["status"] = "committed"
        return record

    async def list_status_events(self, tenant_id, ticket_id):
        return []

    async def create_from_inbound_event(self, tenant_id, channel, external_event_id, event_payload, request):
        self.inbound.append((tenant_id, channel, external_event_id, event_payload, request))
        if external_event_id == "duplicate":
            return False, SimpleNamespace(ticket_id="existing-ticket")
        record = SimpleNamespace(ticket_id=request.ticket_id, version=0, status=TicketStatus.NEW, requester_id=request.requester_id)
        self.items[(tenant_id, request.ticket_id)] = record
        return True, record


class FakeIntakeGraph:
    def __init__(self):
        self.inputs = []
        self.pending = ()
        self.result = {"category": "it", "dispatch_team_id": "team-it", "priority": "normal"}

    async def ainvoke(self, inputs, config):
        self.inputs.append((inputs, config))
        if hasattr(inputs, "resume"):
            self.pending = ()
        return dict(self.result)

    async def aget_state(self, config):
        return SimpleNamespace(tasks=(SimpleNamespace(interrupts=self.pending),) if self.pending else ())


class FakeOperations:
    def __init__(self):
        self.surveys = []
        self.responses = []
        self.outbound = []

    async def append_outbound_message(self, **kwargs):
        self.outbound.append(kwargs)
        return True

    async def get_ticket_overview(self, tenant_id, ticket_id):
        return {"sla": None, "survey": None, "messages": [], "assignments": []}

    async def ensure_sla_for_ticket(self, **kwargs):
        return False

    async def pause_sla(self, tenant_id, ticket_id, *, reason):
        return True

    async def resume_sla(self, tenant_id, ticket_id, *, resumed_at):
        return True

    async def mark_first_response(self, tenant_id, ticket_id, *, at=None):
        return True

    async def create_survey(self, **kwargs):
        self.surveys.append(kwargs)
        return True

    async def respond_survey(self, **kwargs):
        self.responses.append(kwargs)
        return True


class EmptyGraph:
    async def astream(self, *_args, **_kwargs):
        if False:
            yield {}


def load_app(monkeypatch, *, dingtalk=False):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("TENANT_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    if dingtalk:
        monkeypatch.setenv("DINGTALK_TENANT_ID", "tenant-channel")
        monkeypatch.setenv("DINGTALK_APP_SECRET", "dingtalk-secret")
    else:
        monkeypatch.delenv("DINGTALK_TENANT_ID", raising=False)
        monkeypatch.delenv("DINGTALK_APP_SECRET", raising=False)
    module = importlib.reload(importlib.import_module("backend.app"))
    tickets = FakeTickets()
    intake = FakeIntakeGraph()
    operations = FakeOperations()
    tickets.operations = operations
    runtime = SimpleNamespace(
        graph=EmptyGraph(),
        tickets=tickets,
        intake_graph=intake,
        ticket_operations=operations,
    )

    @asynccontextmanager
    async def fake_runtime_context(_settings, **_kwargs):
        yield runtime

    module.runtime_context = fake_runtime_context
    return module, tickets, intake


def headers(*scopes, tenant="tenant-a", user="user-1"):
    token = make_tenant_token(tenant, user, SECRET, scopes=scopes)
    return {"Authorization": f"Bearer {token}"}


def test_channel_intake_records_workflow_operation_and_sends_clarification(monkeypatch):
    module, tickets, intake = load_app(monkeypatch)
    intake.result = {"__interrupt__": (), "missing_fields": ["impact"]}
    intake.pending = (Interrupt(id="interrupt-chan", value={"kind": "ticket_clarification", "question": "补充影响范围", "allowed_actions": ["provide_information"], "expected_actor": "customer", "expected_actor_id": "user-1", "ticket_id": "ticket-1"}),)
    with TestClient(module.app) as client:
        response = client.post(
            "/integrations/wecom/events",
            headers=headers("ticket:channel", user="wecom-adapter"),
            json={
                "external_event_id": "event-1",
                "requester_id": "external-user",
                "title": "Login failure",
                "content": "Cannot sign in",
                "payload": {"raw": "message"},
            },
        )

    assert response.status_code == 200
    assert response.json()["intake"]["interrupt"]["interrupt_id"] == "interrupt-chan"
    assert tickets.workflow_runs[("tenant-a", response.json()["ticket_id"], "channel:event-1")]["status"] == "committed"
    assert tickets.operations.outbound[0]["idempotency_key"] == "clarify:" + response.json()["ticket_id"]
    assert "补充" in tickets.operations.outbound[0]["content"]


def test_create_ticket_derives_tenant_requester_and_actor_from_token(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    with TestClient(module.app) as client:
        response = client.post(
            "/tickets",
            headers=headers("ticket:customer"),
            json={"ticket_id": "ticket-1", "title": "Login failure"},
        )

    assert response.status_code == 201
    tenant_id, request = tickets.created[0]
    assert tenant_id == "tenant-a"
    assert request.requester_id == "user-1"
    assert request.actor_id == "user-1"
    assert request.actor_type == ActorType.CUSTOMER


def test_ticket_create_requires_customer_scope_and_rejects_identity_injection(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch)
    with TestClient(module.app) as client:
        forbidden = client.post(
            "/tickets",
            headers=headers("ticket:agent"),
            json={"ticket_id": "ticket-1", "title": "Login failure"},
        )
        invalid = client.post(
            "/tickets",
            headers=headers("ticket:customer"),
            json={"ticket_id": "ticket-1", "title": "Login failure", "requester_id": "victim"},
        )

    assert forbidden.status_code == 403
    assert invalid.status_code == 422


def test_customer_cannot_read_or_transition_another_customers_ticket(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-2", version=0, status=TicketStatus.NEW
    )
    with TestClient(module.app) as client:
        read = client.get("/tickets/ticket-1", headers=headers("ticket:customer"))
        transition = client.post(
            "/tickets/ticket-1/transitions",
            headers=headers("ticket:customer"),
            json={"action": "cancel", "actor_type": "customer", "expected_version": 0},
        )

    assert read.status_code == 404
    assert transition.status_code == 404
    assert tickets.transitions == []


def test_agent_transition_uses_authenticated_actor_and_scopes(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-2", version=2, status=TicketStatus.QUEUED
    )
    with TestClient(module.app) as client:
        response = client.post(
            "/tickets/ticket-1/transitions",
            headers=headers("ticket:agent", user="agent-7"),
            json={"action": "assign", "actor_type": "agent", "expected_version": 2},
        )

    assert response.status_code == 200
    tenant_id, command, scopes = tickets.transitions[0]
    assert tenant_id == "tenant-a"
    assert command.actor_id == "agent-7"
    assert scopes == frozenset({"ticket:agent"})


def test_channel_endpoint_requires_scope_and_uses_atomic_repository_method(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    body = {
        "external_event_id": "event-1",
        "requester_id": "external-user",
        "title": "Login failure",
        "content": "Cannot sign in",
        "payload": {"raw": "message"},
    }
    with TestClient(module.app) as client:
        forbidden = client.post(
            "/integrations/wecom/events",
            headers=headers("ticket:customer"),
            json=body,
        )
        created = client.post(
            "/integrations/wecom/events",
            headers=headers("ticket:channel", user="wecom-adapter"),
            json=body,
        )

    assert forbidden.status_code == 403
    assert created.status_code == 200
    tenant_id, channel, event_id, payload, request = tickets.inbound[0]
    assert (tenant_id, channel, event_id) == ("tenant-a", "wecom", "event-1")
    assert payload == {"raw": "message"}
    assert request.actor_id == "wecom-adapter"
    assert request.requester_id == "external-user"
    assert [entry[1].action.value for entry in tickets.transitions] == [
        "start_intake",
        "classify",
        "queue",
    ]


def test_pending_interrupt_endpoint_rehydrates_checkpoint_after_refresh(monkeypatch):
    module, tickets, intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=2, status=TicketStatus.AWAITING_CUSTOMER
    )
    intake.pending = (Interrupt(id="interrupt-refresh", value={"kind": "ticket_clarification", "ticket_id": "ticket-1", "question": "补充影响范围", "allowed_actions": ["provide_information"], "expected_actor": "customer", "expected_actor_id": "user-1"}),)
    with TestClient(module.app) as client:
        response = client.get("/tickets/ticket-1/pending-interrupt", headers=headers("ticket:customer"))
    assert response.status_code == 200
    assert response.json()["interrupt"]["interrupt_id"] == "interrupt-refresh"
    assert response.json()["interrupt"]["question"] == "补充影响范围"


def test_intake_synchronizes_completed_graph_to_queued_ticket(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=0, status=TicketStatus.NEW
    )
    with TestClient(module.app) as client:
        response = client.post(
            "/tickets/ticket-1/intake",
            headers=headers("ticket:customer"),
            json={
                "operation_id": "op-intake-complete",
                "text": "SSO login failure",
                "fields": {"title": "SSO", "description": "failure"},
                "expected_version": 0,
            },
        )

    assert response.status_code == 200
    assert response.json()["ticket"]["status"] == "queued"
    assert response.json()["ticket"]["version"] == 3
    assert [entry[1].action.value for entry in tickets.transitions] == [
        "start_intake",
        "classify",
        "queue",
    ]
    assert [entry[1].expected_version for entry in tickets.transitions] == [0, 1, 2]


def test_intake_operation_retries_recorded_intent_without_rerunning_graph(monkeypatch):
    module, tickets, intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=0, status=TicketStatus.NEW
    )
    tickets.fail_next_transition = True
    body = {
        "operation_id": "op-fault-injection",
        "text": "SSO login failure",
        "fields": {"title": "SSO", "description": "failure"},
        "expected_version": 0,
    }
    with TestClient(module.app) as client:
        first = client.post("/tickets/ticket-1/intake", headers=headers("ticket:customer"), json=body)
        second = client.post("/tickets/ticket-1/intake", headers=headers("ticket:customer"), json=body)

    assert first.status_code == 500
    assert second.status_code == 200
    assert tickets.items[("tenant-a", "ticket-1")].status == TicketStatus.QUEUED
    assert tickets.items[("tenant-a", "ticket-1")].version == 3
    assert len(intake.inputs) == 1
    assert [item[1].action.value for item in tickets.transitions] == ["start_intake", "classify", "queue"]
    assert tickets.workflow_runs[("tenant-a", "ticket-1", "op-fault-injection")]["status"] == "committed"


def test_intake_synchronizes_interrupt_to_awaiting_customer(monkeypatch):
    module, tickets, intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=0, status=TicketStatus.NEW
    )
    intake.result = {"__interrupt__": (), "missing_fields": ["impact"]}
    intake.pending = (
        Interrupt(
            id="interrupt-1",
            value={
                "ticket_id": "ticket-1",
                "expected_actor": "customer",
                "expected_actor_id": "user-1",
                "allowed_actions": ["provide_information"],
                "question": "请补充影响范围",
            },
        ),
    )
    with TestClient(module.app) as client:
        response = client.post(
            "/tickets/ticket-1/intake",
            headers=headers("ticket:customer"),
            json={"operation_id": "op-intake-interrupt", "text": "SSO failure", "fields": {}, "expected_version": 0},
        )

    assert response.status_code == 200
    assert response.json()["ticket"]["status"] == "awaiting_customer"
    assert response.json()["ticket"]["version"] == 2
    assert response.json()["interrupt"]["interrupt_id"] == "interrupt-1"
    assert response.json()["interrupt"]["question"] == "请补充影响范围"
    assert response.json()["state"]["missing_fields"] == ["impact"]


def test_typed_resume_validates_real_interrupt_actor_and_version(monkeypatch):
    module, tickets, intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=3, status=TicketStatus.AWAITING_CUSTOMER
    )
    intake.pending = (
        Interrupt(
            id="interrupt-1",
            value={
                "ticket_id": "ticket-1",
                "expected_actor": "customer",
                "expected_actor_id": "user-1",
                "allowed_actions": ["provide_information"],
            },
        ),
    )
    body = {
        "operation_id": "op-resume-complete",
        "interrupt_id": "interrupt-1",
        "ticket_id": "ticket-1",
        "actor_type": "customer",
        "actor_id": "user-1",
        "action": "provide_information",
        "expected_version": 3,
        "payload": {"fields": {"impact": "one user"}},
    }
    with TestClient(module.app) as client:
        response = client.post(
            "/tickets/ticket-1/resume",
            headers=headers("ticket:customer"),
            json=body,
        )

    assert response.status_code == 200
    assert response.json()["ticket"]["status"] == "queued"
    assert response.json()["ticket"]["version"] == 6
    assert response.json()["state"]["category"] == "it"
    assert intake.inputs[-1][0].resume["action"] == "provide_information"
    assert intake.inputs[-1][1]["configurable"]["thread_id"] == "helpdesk:tenant-a:ticket-1"


def test_typed_resume_rejects_stale_version_and_non_customer_actor(monkeypatch):
    module, tickets, intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=4, status=TicketStatus.AWAITING_CUSTOMER
    )
    intake.pending = (
        Interrupt(
            id="interrupt-1",
            value={
                "ticket_id": "ticket-1",
                "expected_actor": "customer",
                "expected_actor_id": "user-1",
                "allowed_actions": ["provide_information"],
            },
        ),
    )
    with TestClient(module.app) as client:
        stale = client.post(
            "/tickets/ticket-1/resume",
            headers=headers("ticket:customer"),
            json={
                "operation_id": "op-resume-stale",
                "interrupt_id": "interrupt-1",
                "ticket_id": "ticket-1",
                "actor_type": "customer",
                "actor_id": "user-1",
                "action": "provide_information",
                "expected_version": 3,
            },
        )
        wrong_actor = client.post(
            "/tickets/ticket-1/resume",
            headers=headers("ticket:customer", "ticket:agent"),
            json={
                "operation_id": "op-resume-wrong-actor",
                "interrupt_id": "interrupt-1",
                "ticket_id": "ticket-1",
                "actor_type": "agent",
                "actor_id": "agent-injected",
                "action": "provide_information",
                "expected_version": 4,
            },
        )

    assert stale.status_code == 409
    assert wrong_actor.status_code == 403


def test_ticket_list_scopes_customer_to_requester_and_allows_agent_filters(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    with TestClient(module.app) as client:
        customer = client.get(
            "/tickets?status=new&limit=20",
            headers=headers("ticket:customer"),
        )
        agent = client.get(
            "/tickets?status=queued&assigned_team_id=team-it&limit=10",
            headers=headers("ticket:agent", user="agent-1"),
        )

    assert customer.status_code == 200
    assert agent.status_code == 200
    customer_call = tickets.list_calls[0][1]
    agent_call = tickets.list_calls[1][1]
    assert customer_call["requester_id"] == "user-1"
    assert customer_call["assigned_team_id"] is None
    assert customer_call["statuses"] == (TicketStatus.NEW,)
    assert agent_call["requester_id"] is None
    assert agent_call["assigned_team_id"] == "team-it"
    assert agent_call["statuses"] == (TicketStatus.QUEUED,)


def test_ticket_list_rejects_invalid_cursor(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch)
    with TestClient(module.app) as client:
        response = client.get(
            "/tickets?cursor=not-base64",
            headers=headers("ticket:agent"),
        )
    assert response.status_code == 422


def test_survey_create_and_response_enforce_roles_and_ownership(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=1, status=TicketStatus.RESOLVED
    )
    with TestClient(module.app) as client:
        forbidden = client.post(
            "/tickets/ticket-1/survey",
            headers=headers("ticket:customer"),
            json={"expires_in_days": 7},
        )
        created = client.post(
            "/tickets/ticket-1/survey",
            headers=headers("ticket:agent", user="agent-1"),
            json={"expires_in_days": 7},
        )
        responded = client.post(
            "/tickets/ticket-1/survey/survey-1/response",
            headers=headers("ticket:customer"),
            json={"score": 5, "feedback": "resolved"},
        )

    assert forbidden.status_code == 403
    assert created.status_code == 201
    assert responded.status_code == 200
    assert tickets.operations.surveys[0]["tenant_id"] == "tenant-a"
    assert tickets.operations.responses[0]["score"] == 5


def test_ticket_api_is_rate_limited_per_authenticated_principal(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=0, status=TicketStatus.NEW
    )
    with TestClient(module.app) as client:
        module.app.state.rate_limiter = InMemoryRateLimiter(capacity=1, window_seconds=60)
        allowed = client.get("/tickets/ticket-1", headers=headers("ticket:customer"))
        rejected = client.get("/tickets/ticket-1", headers=headers("ticket:customer"))
        other_tenant = client.get(
            "/tickets/ticket-1",
            headers=headers("ticket:customer", tenant="tenant-b"),
        )

    assert allowed.status_code == 200
    assert rejected.status_code == 429
    assert other_tenant.status_code == 404


def test_vendor_webhook_rejects_oversized_body_before_signature(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch, dingtalk=True)
    timestamp = str(int(time.time() * 1000))
    with TestClient(module.app) as client:
        response = client.post(
            f"/integrations/dingtalk/webhook?timestamp={timestamp}&sign=bad",
            content=b"x" * (256 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413


def _dingtalk_sign(timestamp: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode(),
        f"{timestamp}\n{secret}".encode(),
        hashlib.sha256,
    ).digest()
    return quote(base64.b64encode(digest).decode(), safe="")


def test_dingtalk_webhook_uses_signature_auth_and_configured_tenant(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch, dingtalk=True)
    timestamp = str(int(time.time() * 1000))
    body = {
        "msgId": "ding-msg-1",
        "senderStaffId": "external-user",
        "text": {"content": "VPN is unavailable"},
    }
    with TestClient(module.app) as client:
        response = client.post(
            f"/integrations/dingtalk/webhook?timestamp={timestamp}&sign={_dingtalk_sign(timestamp, 'dingtalk-secret')}",
            content=json.dumps(body, separators=(",", ":")),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    tenant_id, channel, event_id, payload, request = tickets.inbound[0]
    assert (tenant_id, channel, event_id) == ("tenant-channel", "dingtalk", "ding-msg-1")
    assert request.requester_id == "external-user"
    assert request.actor_id == "dingtalk-webhook"
    assert payload["text"]["content"] == "VPN is unavailable"


def test_dingtalk_webhook_rejects_bad_signature_and_disabled_configuration(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch, dingtalk=True)
    timestamp = str(int(time.time() * 1000))
    body = {"msgId": "m", "senderStaffId": "u", "content": "help"}
    with TestClient(module.app) as client:
        bad = client.post(
            f"/integrations/dingtalk/webhook?timestamp={timestamp}&sign=bad",
            json=body,
        )
    assert bad.status_code == 401

    disabled_module, _tickets, _intake = load_app(monkeypatch, dingtalk=False)
    with TestClient(disabled_module.app) as client:
        disabled = client.post(
            f"/integrations/dingtalk/webhook?timestamp={timestamp}&sign=bad",
            json=body,
        )
    assert disabled.status_code == 503
