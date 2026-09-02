import base64
import hashlib
import hmac
import importlib
import json
import struct
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from langgraph.types import Interrupt

from backend.rate_limit import InMemoryRateLimiter
from backend.security import make_tenant_token
from backend.ticket_api import _classify_category
from backend.tickets import AssetBindingError
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
        self.assets = None
        self.seen_inbound = set()
        self.inbound = {}
        self.inbound_calls = []
        self.attachments = []

    async def create(self, tenant_id, request):
        # 复刻数据访问层的客户资产归属校验（与 backend/tickets/repository.py 一致）。
        if request.asset_id is not None and request.actor_type == ActorType.CUSTOMER:
            asset = (
                await self.assets.get(tenant_id, request.asset_id)
                if self.assets is not None
                else None
            )
            if (
                asset is None
                or asset.owner_user_id is None
                or asset.owner_user_id != request.requester_id
            ):
                raise AssetBindingError("资产不存在或不属于当前用户")
        self.created.append((tenant_id, request))
        record = SimpleNamespace(
            tenant_id=tenant_id,
            ticket_id=request.ticket_id,
            requester_id=request.requester_id,
            status=TicketStatus.NEW,
            version=0,
            asset_id=request.asset_id,
        )
        self.items[(tenant_id, request.ticket_id)] = record
        return record

    async def get(self, tenant_id, ticket_id):
        return self.items.get((tenant_id, ticket_id))

    async def bind_asset(self, tenant_id, ticket_id, asset_id):
        record = self.items[(tenant_id, ticket_id)]
        record.asset_id = asset_id
        return record

    async def unbind_asset(self, tenant_id, ticket_id):
        record = self.items[(tenant_id, ticket_id)]
        record.asset_id = None
        return record

    async def list_tickets(self, tenant_id, **kwargs):
        self.list_calls.append((tenant_id, kwargs))
        items = [item for (tid, _), item in self.items.items() if tid == tenant_id]
        requester_id = kwargs.get("requester_id")
        if requester_id is not None:
            items = [item for item in items if getattr(item, "requester_id", None) == requester_id]
        asset_id = kwargs.get("asset_id")
        if asset_id is not None:
            items = [item for item in items if getattr(item, "asset_id", None) == asset_id]
        return items[: kwargs["limit"]]

    async def start_workflow_operation(self, **kwargs):
        key = (kwargs["tenant_id"], kwargs["ticket_id"], kwargs["operation_id"])
        run = self.workflow_runs.setdefault(key, {"status": "started", "intent": None, **kwargs})
        return run

    async def get_workflow_operation(self, *, tenant_id, ticket_id, operation_id):
        return self.workflow_runs.get((tenant_id, ticket_id, operation_id))

    async def record_workflow_intent(
        self, *, tenant_id, ticket_id, operation_id, intent, checkpoint_id=None
    ):
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

    # ---- 渠道入站快速 ACK（异步 Worker 处理）：登记/查询/关联 ----
    async def register_inbound_event(self, tenant_id, channel, external_event_id, payload):
        key = (tenant_id, channel, external_event_id)
        self.inbound_calls.append((tenant_id, channel, external_event_id, payload))
        if key in self.inbound:
            return SimpleNamespace(
                created=False,
                tenant_id=tenant_id,
                channel=channel,
                external_event_id=external_event_id,
                payload_hash="hash",
                ticket_id=self.inbound[key]["ticket_id"],
                status=self.inbound[key]["status"],
                attempts=0,
            )
        self.inbound[key] = {
            "status": "received",
            "ticket_id": None,
            "payload": payload,
            "attempts": 0,
        }
        return SimpleNamespace(
            created=True,
            tenant_id=tenant_id,
            channel=channel,
            external_event_id=external_event_id,
            payload_hash="hash",
            ticket_id=None,
            status="received",
            attempts=0,
        )

    async def get_inbound_event(self, tenant_id, channel, external_event_id):
        return self.inbound.get((tenant_id, channel, external_event_id))

    async def list_inbound_events(self, tenant_id, external_event_id):
        return [
            {
                "tenant_id": tenant_id,
                "channel": channel,
                "external_event_id": external_event_id,
                "status": record["status"],
                "ticket_id": record["ticket_id"],
                "attempts": record["attempts"],
                "error_code": None,
                "received_at": None,
                "processed_at": None,
            }
            for (tid, channel, event_id), record in self.inbound.items()
            if tid == tenant_id and event_id == external_event_id
        ]

    async def attach_inbound_event(self, tenant_id, channel, external_event_id, ticket_id):
        record = self.inbound.get((tenant_id, channel, external_event_id))
        if record is not None:
            record["ticket_id"] = ticket_id
        self.attachments.append((tenant_id, channel, external_event_id, ticket_id))


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
        return SimpleNamespace(
            tasks=(SimpleNamespace(interrupts=self.pending),) if self.pending else ()
        )


class FakeOperations:
    def __init__(self):
        self.surveys = []
        self.responses = []
        self.outbound = []
        self.sla_calls = []
        self.fail_sla_once = False

    async def append_outbound_message(self, **kwargs):
        self.outbound.append(kwargs)
        return True

    async def get_ticket_overview(self, tenant_id, ticket_id):
        return {"sla": None, "survey": None, "messages": [], "assignments": []}

    async def ensure_sla_for_ticket(self, **kwargs):
        self.sla_calls.append(kwargs)
        if self.fail_sla_once:
            self.fail_sla_once = False
            raise RuntimeError("injected SLA failure")
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


class FakeAudit:
    def __init__(self):
        self.admin_events = []

    async def start_run(self, *_args, **_kwargs):
        return None

    async def finish_run(self, *_args, **_kwargs):
        return True

    async def record_event(self, *_args, **_kwargs):
        return None

    async def record_admin_event(self, **kwargs):
        self.admin_events.append(kwargs)

    async def list_admin_events(self, **kwargs):
        return self.admin_events


class FakeAssets:
    def __init__(self):
        self.items = {}
        self.created = []
        self.updated = []
        self.deleted = []

    async def list_assets(self, tenant_id, **kwargs):
        items = [item for (tid, _), item in self.items.items() if tid == tenant_id]
        owner = kwargs.get("owner_user_id")
        if owner is not None:
            items = [item for item in items if item.owner_user_id == owner]
        return items

    async def get(self, tenant_id, asset_id):
        record = self.items.get((tenant_id, asset_id))
        return record if record is not None and not record.is_deleted else None

    async def create(self, tenant_id, request):
        self.created.append((tenant_id, request))
        record = SimpleNamespace(
            tenant_id=tenant_id,
            asset_id=request.asset_id,
            asset_no=request.asset_no,
            asset_type=request.asset_type,
            name=request.name,
            hostname=request.hostname,
            ip_address=request.ip_address,
            status=request.status.value,
            owner_user_id=request.owner_user_id,
            is_deleted=False,
        )
        self.items[(tenant_id, request.asset_id)] = record
        return record

    async def update(self, tenant_id, asset_id, changes):
        record = self.items[(tenant_id, asset_id)]
        self.updated.append((tenant_id, asset_id, changes))
        for field, value in changes.model_dump(exclude_unset=True).items():
            if field == "status" and value is not None:
                value = value.value
            setattr(record, field, value)
        return record

    async def soft_delete(self, tenant_id, asset_id):
        record = self.items.get((tenant_id, asset_id))
        if record is None:
            return False
        record.is_deleted = True
        self.deleted.append((tenant_id, asset_id))
        return True


class FakeItPolicies:
    def __init__(self):
        self.items = {}

    async def get(self, tenant_id, category):
        return self.items.get((tenant_id, category))

    async def upsert(self, tenant_id, policy):
        record = SimpleNamespace(
            tenant_id=tenant_id,
            category=policy.category,
            policy_id=policy.policy_id,
            required_fields=policy.required_fields,
            default_priority=policy.default_priority,
            auto_answer_enabled=policy.auto_answer_enabled,
            approval_required=policy.approval_required,
            active=policy.active,
        )
        self.items[(tenant_id, policy.category)] = record
        return record

    async def list_active(self, tenant_id):
        return [item for (tid, _), item in self.items.items() if tid == tenant_id]

    async def delete(self, tenant_id, category):
        return self.items.pop((tenant_id, category), None) is not None




class FakeChannelIdentities:
    def __init__(self):
        self.items = {}

    async def get(self, tenant_id, channel, requester_id):
        return self.items.get((tenant_id, channel, requester_id))

    async def upsert(self, tenant_id, identity):
        record = SimpleNamespace(
            tenant_id=tenant_id,
            channel=identity.channel,
            requester_id=identity.requester_id,
            departments=identity.departments,
            asset_id=identity.asset_id,
            internal=identity.internal,
            mapping_source=identity.mapping_source,
            active=identity.active,
        )
        self.items[(tenant_id, identity.channel, identity.requester_id)] = record
        return record

    async def list_admin(self, tenant_id, *, limit=100):
        return [
            record
            for (tid, _channel, _requester), record in self.items.items()
            if tid == tenant_id
        ][:limit]

    async def delete(self, tenant_id, channel, requester_id):
        key = (tenant_id, channel, requester_id)
        if key not in self.items:
            return False
        del self.items[key]
        return True

class FakeKnowledge:
    def __init__(self):
        self.put = []
        self.published = []
        self.retired = []

    async def put_document(self, tenant_id, document, chunks):
        self.put.append((tenant_id, document, chunks))

    async def publish_document_version(self, tenant_id, document_id, version):
        self.published.append((tenant_id, document_id, version))

    async def retire_document(self, tenant_id, document_id):
        self.retired.append((tenant_id, document_id))
        return True

    async def list_documents(self, tenant_id, **kwargs):
        return []


def load_app(monkeypatch, *, dingtalk=False, wecom=False):
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
    if wecom:
        monkeypatch.setenv("WECOM_TENANT_ID", "tenant-channel")
        monkeypatch.setenv("WECOM_TOKEN", "wecom-token")
        monkeypatch.setenv("WECOM_ENCODING_AES_KEY", WECOM_ENCODING_KEY)
        monkeypatch.setenv("WECOM_CORP_ID", "corp-1")
    else:
        monkeypatch.delenv("WECOM_TENANT_ID", raising=False)
        monkeypatch.delenv("WECOM_TOKEN", raising=False)
        monkeypatch.delenv("WECOM_ENCODING_AES_KEY", raising=False)
        monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    module = importlib.reload(importlib.import_module("backend.app"))
    tickets = FakeTickets()
    intake = FakeIntakeGraph()
    operations = FakeOperations()
    tickets.operations = operations
    assets = FakeAssets()
    tickets.assets = assets
    runtime = SimpleNamespace(
        graph=EmptyGraph(),
        tickets=tickets,
        intake_graph=intake,
        ticket_operations=operations,
        assets=assets,
        it_policies=FakeItPolicies(),
        channel_identities=FakeChannelIdentities(),
        knowledge=FakeKnowledge(),
        audit=FakeAudit(),
    )

    @asynccontextmanager
    async def fake_runtime_context(_settings, **_kwargs):
        yield runtime

    module.runtime_context = fake_runtime_context
    return module, tickets, intake


def headers(*scopes, tenant="tenant-a", user="user-1", departments=(), internal=False):
    token = make_tenant_token(
        tenant,
        user,
        SECRET,
        scopes=scopes,
        departments=tuple(departments),
        internal=internal,
    )
    return {"Authorization": f"Bearer {token}"}


def test_channel_event_acks_fast_without_processing(monkeypatch):
    """POST 快速 ACK：登记事件立即返回 202，业务（受理/追问）不在 HTTP 请求中执行。"""
    module, tickets, _intake = load_app(monkeypatch)
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

    assert response.status_code == 202
    body = response.json()
    assert body == {"accepted": True, "event_id": "event-1", "created": True}
    assert tickets.workflow_runs == {}  # 未在 HTTP 请求中启动受理
    assert tickets.operations.outbound == []  # 未在 HTTP 请求中发澄清
    assert ("tenant-a", "wecom", "event-1") in tickets.inbound  # 事件已登记
    assert tickets.inbound[("tenant-a", "wecom", "event-1")]["status"] == "received"


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


def test_channel_endpoint_requires_scope_and_registers_event(monkeypatch):
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
    assert created.status_code == 202
    assert created.json() == {"accepted": True, "event_id": "event-1", "created": True}
    tenant_id, channel, event_id, payload = tickets.inbound_calls[0]
    assert (tenant_id, channel, event_id) == ("tenant-a", "wecom", "event-1")
    assert payload["raw"] == {"raw": "message"}
    assert payload["requester_id"] == "external-user"
    assert tickets.transitions == []  # 建单/受理留给 InboundWorker


def test_inbound_event_status_query_and_idempotent_registration(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    body = {
        "external_event_id": "event-1",
        "requester_id": "external-user",
        "title": "Login failure",
        "content": "Cannot sign in",
        "payload": {},
    }
    channel_headers = headers("ticket:channel", user="wecom-adapter")
    with TestClient(module.app) as client:
        first = client.post("/integrations/wecom/events", headers=channel_headers, json=body)
        second = client.post("/integrations/wecom/events", headers=channel_headers, json=body)
        status = client.get("/integrations/events/event-1", headers=channel_headers)

    assert first.status_code == 202
    assert first.json()["created"] is True
    assert second.status_code == 202
    assert second.json()["created"] is False  # 同 event_id 幂等，返回原记录
    assert status.status_code == 200
    assert status.json()["items"][0]["status"] == "received"
    assert status.json()["items"][0]["channel"] == "wecom"


def test_intake_completes_to_queued_and_creates_sla_instance(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1",
        requester_id="user-1",
        version=0,
        status=TicketStatus.NEW,
        channel="web",
        category=None,
    )
    with TestClient(module.app) as client:
        response = client.post(
            "/tickets/ticket-1/intake",
            headers=headers("ticket:customer"),
            json={
                "operation_id": "op-sla-create",
                "text": "VPN cannot connect",
                "fields": {"title": "VPN", "description": "cannot connect"},
                "expected_version": 0,
            },
        )
    assert response.status_code == 200
    assert response.json()["ticket"]["status"] == "queued"
    assert tickets.operations.sla_calls
    call = tickets.operations.sla_calls[0]
    assert call["tenant_id"] == "tenant-a"
    assert call["ticket_id"] == "ticket-1"


def test_intake_config_carries_identity_and_asset_from_principal(monkeypatch):
    """Day 4：受理图 config 必须携带认证主体身份与资产，而非只传 tenant_id。"""
    module, tickets, intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1",
        requester_id="user-1",
        version=0,
        status=TicketStatus.NEW,
        channel="web",
        category=None,
        asset_id="laptop-1",
    )
    with TestClient(module.app) as client:
        response = client.post(
            "/tickets/ticket-1/intake",
            headers=headers("ticket:customer", departments=("it",)),
            json={
                "operation_id": "op-identity",
                "text": "VPN cannot connect",
                "fields": {"title": "VPN", "description": "cannot connect"},
                "expected_version": 0,
            },
        )
    assert response.status_code == 200
    config = intake.inputs[0][1]
    assert config["configurable"]["user_id"] == "user-1"
    assert config["configurable"]["departments"] == ["it"]
    assert config["configurable"]["asset_id"] == "laptop-1"


def test_pending_interrupt_endpoint_rehydrates_checkpoint_after_refresh(monkeypatch):
    module, tickets, intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1",
        requester_id="user-1",
        version=2,
        status=TicketStatus.AWAITING_CUSTOMER,
    )
    intake.pending = (
        Interrupt(
            id="interrupt-refresh",
            value={
                "kind": "ticket_clarification",
                "ticket_id": "ticket-1",
                "question": "补充影响范围",
                "allowed_actions": ["provide_information"],
                "expected_actor": "customer",
                "expected_actor_id": "user-1",
            },
        ),
    )
    with TestClient(module.app) as client:
        response = client.get(
            "/tickets/ticket-1/pending-interrupt", headers=headers("ticket:customer")
        )
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
        first = client.post(
            "/tickets/ticket-1/intake", headers=headers("ticket:customer"), json=body
        )
        second = client.post(
            "/tickets/ticket-1/intake", headers=headers("ticket:customer"), json=body
        )

    assert first.status_code == 500
    assert second.status_code == 200
    assert tickets.items[("tenant-a", "ticket-1")].status == TicketStatus.QUEUED
    assert tickets.items[("tenant-a", "ticket-1")].version == 3
    assert len(intake.inputs) == 1
    assert [item[1].action.value for item in tickets.transitions] == [
        "start_intake",
        "classify",
        "queue",
    ]
    assert (
        tickets.workflow_runs[("tenant-a", "ticket-1", "op-fault-injection")]["status"]
        == "committed"
    )


def test_intake_retry_repairs_sla_after_transition_was_committed(monkeypatch):
    """SLA 暂时失败不能让已提交 operation 永久缺 SLA；幂等重试应再次补建。"""
    module, tickets, intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1", requester_id="user-1", version=0, status=TicketStatus.NEW
    )
    tickets.operations.fail_sla_once = True
    body = {
        "operation_id": "op-sla-retry",
        "text": "SSO login failure",
        "fields": {"title": "SSO", "description": "failure"},
        "expected_version": 0,
    }
    with TestClient(module.app) as client:
        first = client.post("/tickets/ticket-1/intake", headers=headers("ticket:customer"), json=body)
        second = client.post("/tickets/ticket-1/intake", headers=headers("ticket:customer"), json=body)

    assert first.status_code == 500
    assert second.status_code == 200
    assert len(tickets.operations.sla_calls) == 2
    assert len(intake.inputs) == 1


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
            json={
                "operation_id": "op-intake-interrupt",
                "text": "SSO failure",
                "fields": {},
                "expected_version": 0,
            },
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
        ticket_id="ticket-1",
        requester_id="user-1",
        version=3,
        status=TicketStatus.AWAITING_CUSTOMER,
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
        ticket_id="ticket-1",
        requester_id="user-1",
        version=4,
        status=TicketStatus.AWAITING_CUSTOMER,
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

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "event_id": "ding-msg-1", "created": True}
    tenant_id, channel, event_id, payload = tickets.inbound_calls[0]
    assert (tenant_id, channel, event_id) == ("tenant-channel", "dingtalk", "ding-msg-1")
    assert payload["requester_id"] == "external-user"
    assert payload["raw"]["text"]["content"] == "VPN is unavailable"


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


# ========== 企业微信 Webhook 端点：真实加解密 + 幂等建单 ==========

WECOM_KEY = b"0123456789abcdef0123456789abcdef"
WECOM_ENCODING_KEY = base64.b64encode(WECOM_KEY).decode("ascii").rstrip("=")


def _encrypt_wecom(message: bytes, corp_id: str) -> str:
    raw = b"0123456789abcdef" + struct.pack("!I", len(message)) + message + corp_id.encode("utf-8")
    pad = 32 - len(raw) % 32
    padded = raw + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(WECOM_KEY), modes.CBC(WECOM_KEY[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def _wecom_webhook(
    content: str | None = None, event: str | None = None
) -> tuple[bytes, str, str, str]:
    if event is not None:
        inner = (
            "<xml><ToUserName>corp-1</ToUserName><FromUserName>ext-user-1</FromUserName>"
            f"<CreateTime>1700000000</CreateTime><MsgType>event</MsgType><Event>{event}</Event></xml>"
        ).encode()
    else:
        inner = (
            f"<xml><FromUserName>ext-user-1</FromUserName><MsgId>wecom-{uuid4().hex}</MsgId>"
            f"<Content>{content}</Content></xml>"
        ).encode()
    encrypted = _encrypt_wecom(inner, "corp-1")
    body = f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode()
    timestamp = str(int(time.time()))
    nonce = "nonce-wecom"
    signature = hashlib.sha1(
        "".join(sorted(("wecom-token", timestamp, nonce, encrypted))).encode()
    ).hexdigest()
    return body, timestamp, nonce, signature


def test_wecom_webhook_verifies_signature_decrypts_and_is_idempotent(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch, wecom=True)
    body, timestamp, nonce, signature = _wecom_webhook("VPN 无法连接")
    with TestClient(module.app) as client:
        first = client.post(
            f"/integrations/wecom/webhook?timestamp={timestamp}&nonce={nonce}&msg_signature={signature}",
            content=body,
            headers={"Content-Type": "application/xml"},
        )
        # 同一回调重放（企业微信重试/并发重复推送）：必须幂等，不重复建单。
        second = client.post(
            f"/integrations/wecom/webhook?timestamp={timestamp}&nonce={nonce}&msg_signature={signature}",
            content=body,
            headers={"Content-Type": "application/xml"},
        )
        bad = client.post(
            f"/integrations/wecom/webhook?timestamp={timestamp}&nonce={nonce}&msg_signature=bad",
            content=body,
            headers={"Content-Type": "application/xml"},
        )
        disabled_module, _tickets, _intake = load_app(monkeypatch, wecom=False)
        with TestClient(disabled_module.app) as client_disabled:
            disabled = client_disabled.post(
                f"/integrations/wecom/webhook?timestamp={timestamp}&nonce={nonce}&msg_signature={signature}",
                content=body,
                headers={"Content-Type": "application/xml"},
            )

    assert first.status_code == 202
    assert first.json() == {"accepted": True, "event_id": first.json()["event_id"], "created": True}
    assert second.status_code == 202
    assert second.json()["created"] is False
    assert bad.status_code == 401
    assert disabled.status_code == 503


def test_wecom_webhook_get_verifies_echostr(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch, wecom=True)
    plaintext = "echostr-value"
    encrypted = _encrypt_wecom(plaintext.encode("utf-8"), "corp-1")
    timestamp = str(int(time.time()))
    nonce = "nonce-get"
    signature = hashlib.sha1(
        "".join(sorted(("wecom-token", timestamp, nonce, encrypted))).encode()
    ).hexdigest()
    with TestClient(module.app) as client:
        ok = client.get(
            "/integrations/wecom/webhook",
            params={
                "timestamp": timestamp,
                "nonce": nonce,
                "msg_signature": signature,
                "echostr": encrypted,
            },
        )
        bad_signature = client.get(
            "/integrations/wecom/webhook",
            params={
                "timestamp": timestamp,
                "nonce": nonce,
                "msg_signature": "bad",
                "echostr": encrypted,
            },
        )
        expired = client.get(
            "/integrations/wecom/webhook",
            params={
                "timestamp": str(int(time.time()) - 3600),
                "nonce": nonce,
                "msg_signature": signature,
                "echostr": encrypted,
            },
        )
        disabled_module, _tickets, _intake = load_app(monkeypatch, wecom=False)
        with TestClient(disabled_module.app) as client_disabled:
            disabled = client_disabled.get(
                "/integrations/wecom/webhook",
                params={
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "msg_signature": signature,
                    "echostr": encrypted,
                },
            )

    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")
    assert ok.text == "echostr-value"
    assert bad_signature.status_code == 401
    assert expired.status_code == 401
    assert disabled.status_code == 503


def test_wecom_webhook_ignores_event_messages_with_ack(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch, wecom=True)
    body, timestamp, nonce, signature = _wecom_webhook(event="enter_agent")
    with TestClient(module.app) as client:
        response = client.post(
            f"/integrations/wecom/webhook?timestamp={timestamp}&nonce={nonce}&msg_signature={signature}",
            content=body,
            headers={"Content-Type": "application/xml"},
        )

    assert response.status_code == 200
    assert response.json() == {"ignored": True, "event": "enter_agent"}
    assert tickets.inbound == {}  # 事件消息不登记入站、不建单
    assert tickets.inbound_calls == []


# ========== 第二阶段：资产 / IT 策略 / 工单资产 / 知识文档管理接口 ==========


def test_assets_api_enforces_scopes_and_supports_crud(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch)
    with TestClient(module.app) as client:
        forbidden = client.post(
            "/assets",
            headers=headers("ticket:customer"),
            json={"asset_id": "asset-1", "asset_no": "A-001", "asset_type": "laptop"},
        )
        created = client.post(
            "/assets",
            headers=headers(
                "ticket:agent",
                "asset:read",
                "asset:write",
                "it-policy:read",
                "it-policy:write",
                "knowledge:write",
            ),
            json={
                "asset_id": "asset-1",
                "asset_no": "A-001",
                "asset_type": "laptop",
                "hostname": "host-a",
                "owner_user_id": "user-1",
            },
        )
        listed = client.get("/assets", headers=headers("asset:read"))
        cleared = client.patch(
            "/assets/asset-1",
            headers=headers("asset:read", "asset:write"),
            json={"hostname": None},
        )
        deleted = client.delete("/assets/asset-1", headers=headers("asset:read", "asset:write"))

    assert forbidden.status_code == 403
    assert created.status_code == 201
    assert created.json()["hostname"] == "host-a"
    assert listed.json()["items"][0]["asset_id"] == "asset-1"
    assert cleared.json()["hostname"] is None
    assert deleted.json()["deleted"] is True


def test_assets_api_customer_cannot_read_or_query_other_users_assets(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch)
    admin_headers = headers("ticket:agent", "asset:read", "asset:write")
    with TestClient(module.app) as client:
        client.post(
            "/assets",
            headers=admin_headers,
            json={
                "asset_id": "asset-other",
                "asset_no": "A-002",
                "asset_type": "laptop",
                "owner_user_id": "user-2",
            },
        )
        client.post(
            "/assets",
            headers=admin_headers,
            json={
                "asset_id": "asset-mine",
                "asset_no": "A-003",
                "asset_type": "laptop",
                "owner_user_id": "user-1",
            },
        )
        customer = headers("asset:read", user="user-1")
        # 客户读取他人资产 -> 404；读取本人资产 -> 200。
        other_read = client.get("/assets/asset-other", headers=customer)
        mine_read = client.get("/assets/asset-mine", headers=customer)
        # 客户查询他人资产工单 -> 404；本人资产工单 -> 200。
        other_tickets = client.get("/assets/asset-other/tickets", headers=customer)
        mine_tickets = client.get("/assets/asset-mine/tickets", headers=customer)
        # 客服不受归属限制。
        agent_read = client.get(
            "/assets/asset-other", headers=headers("asset:read", "ticket:agent", user="agent-1")
        )
        # 客户列表接口只返回本人资产；伪造 owner_user_id 查询参数也不能越权。
        listed = client.get("/assets", headers=customer)
        spoofed = client.get("/assets?owner_user_id=user-2", headers=customer)

    assert other_read.status_code == 404
    assert mine_read.status_code == 200
    assert mine_read.json()["asset_id"] == "asset-mine"
    assert other_tickets.status_code == 404
    assert mine_tickets.status_code == 200
    assert agent_read.status_code == 200
    assert agent_read.json()["owner_user_id"] == "user-2"
    assert [item["asset_id"] for item in listed.json()["items"]] == ["asset-mine"]
    assert [item["asset_id"] for item in spoofed.json()["items"]] == ["asset-mine"]


def test_customer_cannot_bind_other_users_asset_to_ticket(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch)
    admin_headers = headers("ticket:agent", "asset:read", "asset:write")
    with TestClient(module.app) as client:
        client.post(
            "/assets",
            headers=admin_headers,
            json={
                "asset_id": "asset-other",
                "asset_no": "A-004",
                "asset_type": "laptop",
                "owner_user_id": "user-2",
            },
        )
        client.post(
            "/assets",
            headers=admin_headers,
            json={
                "asset_id": "asset-mine",
                "asset_no": "A-005",
                "asset_type": "laptop",
                "owner_user_id": "user-1",
            },
        )
        # 用户 A(user-1)用用户 B(user-2)的资产建单 -> 404；用本人资产建单 -> 201。
        forbidden = client.post(
            "/tickets",
            headers=headers("ticket:customer", user="user-1"),
            json={"ticket_id": "ticket-x", "title": "VPN issue", "asset_id": "asset-other"},
        )
        created = client.post(
            "/tickets",
            headers=headers("ticket:customer", user="user-1"),
            json={"ticket_id": "ticket-y", "title": "VPN issue", "asset_id": "asset-mine"},
        )

    assert forbidden.status_code == 404
    assert created.status_code == 201
    assert created.json()["asset_id"] == "asset-mine"


def test_asset_ticket_list_isolated_by_requester_for_customers(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    admin_headers = headers("ticket:agent", "asset:read", "asset:write")
    tickets.items[("tenant-a", "ticket-a")] = SimpleNamespace(
        ticket_id="ticket-a",
        requester_id="user-1",
        asset_id="asset-1",
        status=TicketStatus.IN_PROGRESS,
    )
    # 客服误把 user-1 的资产关联到 user-2 的工单（历史数据/客服绑定场景）。
    tickets.items[("tenant-a", "ticket-b")] = SimpleNamespace(
        ticket_id="ticket-b",
        requester_id="user-2",
        asset_id="asset-1",
        status=TicketStatus.IN_PROGRESS,
    )
    with TestClient(module.app) as client:
        client.post(
            "/assets",
            headers=admin_headers,
            json={
                "asset_id": "asset-1",
                "asset_no": "A-006",
                "asset_type": "laptop",
                "owner_user_id": "user-1",
            },
        )
        customer = client.get(
            "/assets/asset-1/tickets",
            headers=headers("ticket:customer", "asset:read", user="user-1"),
        )
        agent = client.get(
            "/assets/asset-1/tickets",
            headers=headers("ticket:customer", "ticket:agent", "asset:read", user="agent-1"),
        )

    assert customer.status_code == 200
    assert [item["ticket_id"] for item in customer.json()["items"]] == ["ticket-a"]
    assert agent.status_code == 200
    assert {item["ticket_id"] for item in agent.json()["items"]} == {"ticket-a", "ticket-b"}


def test_intake_classify_persists_full_subcategory_category(monkeypatch):
    """受理层：分类结果 {"category": "it", "subcategory": "vpn"} 持久化为 it.vpn。

    该值正是 SLA 解析链（it.vpn -> it -> 默认）的输入，覆盖 ticket_api 到
    SLA 选择的关键输入。
    """
    from backend.ticket_api import _intake_outcome_commands
    from src.my_agent.helpdesk import TicketAction

    result = {"category": "it", "subcategory": "vpn", "priority": "normal"}
    commands = _intake_outcome_commands(
        ticket_id="ticket-1", actor_id="intake-agent", expected_version=1, result=result
    )
    classify = next(command for command in commands if command.action == TicketAction.CLASSIFY)
    assert classify.payload["category"] == "it.vpn"

    assert _classify_category({"category": "it", "subcategory": "general"}) == "it"
    assert _classify_category({"category": "it"}) == "it"
    assert _classify_category({"category": "other"}) == "other"


def test_it_policy_admin_api_enforces_scopes_and_upserts(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch)
    admin_headers = headers(
        "ticket:agent",
        "asset:read",
        "asset:write",
        "it-policy:read",
        "it-policy:write",
        "knowledge:write",
    )
    with TestClient(module.app) as client:
        forbidden = client.get("/admin/it/policies/it.vpn", headers=headers("ticket:customer"))
        missing = client.get("/admin/it/policies/it.vpn", headers=headers("it-policy:read"))
        upserted = client.put(
            "/admin/it/policies/it.vpn",
            headers=admin_headers,
            json={"category": "it.vpn", "policy_id": "sla-vpn", "default_priority": "high"},
        )
        fetched = client.get("/admin/it/policies/it.vpn", headers=headers("it-policy:read"))

    assert forbidden.status_code == 403
    assert missing.status_code == 404
    assert upserted.status_code == 200
    assert upserted.json()["policy_id"] == "sla-vpn"
    assert fetched.json()["default_priority"] == "high"


def test_ticket_asset_bind_and_unbind_require_agent_scope(monkeypatch):
    module, tickets, _intake = load_app(monkeypatch)
    tickets.items[("tenant-a", "ticket-1")] = SimpleNamespace(
        ticket_id="ticket-1",
        requester_id="user-1",
        version=1,
        status=TicketStatus.IN_PROGRESS,
        asset_id=None,
    )
    with TestClient(module.app) as client:
        forbidden = client.post(
            "/tickets/ticket-1/asset",
            headers=headers("ticket:customer"),
            json={"asset_id": "asset-1"},
        )
        bound = client.post(
            "/tickets/ticket-1/asset",
            headers=headers("ticket:agent"),
            json={"asset_id": "asset-1"},
        )
        unbound = client.delete("/tickets/ticket-1/asset", headers=headers("ticket:agent"))

    assert forbidden.status_code == 403
    assert bound.json()["asset_id"] == "asset-1"
    assert unbound.json()["asset_id"] is None


def test_knowledge_documents_api_enforces_write_scope_and_publishes(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch)
    admin_headers = headers(
        "ticket:agent",
        "asset:read",
        "asset:write",
        "it-policy:read",
        "it-policy:write",
        "knowledge:write",
    )
    with TestClient(module.app) as client:
        forbidden = client.post(
            "/knowledge/documents",
            headers=headers("ticket:agent"),
            json={
                "document": {
                    "document_id": "doc-1",
                    "version": 1,
                    "title": "VPN",
                    "status": "draft",
                },
                "chunks": [{"chunk_id": "c1", "ordinal": 0, "content": "vpn setup"}],
            },
        )
        created = client.post(
            "/knowledge/documents",
            headers=admin_headers,
            json={
                "document": {
                    "document_id": "doc-1",
                    "version": 1,
                    "title": "VPN",
                    "status": "draft",
                },
                "chunks": [{"chunk_id": "c1", "ordinal": 0, "content": "vpn setup"}],
            },
        )
        published = client.post(
            "/knowledge/documents/doc-1/publish",
            headers=admin_headers,
            json={"version": 1},
        )
        retired = client.post(
            "/knowledge/documents/doc-1/retire",
            headers=admin_headers,
        )

    assert forbidden.status_code == 403
    assert created.status_code == 201
    assert published.json()["status"] == "published"
    assert retired.json()["status"] == "retired"


def test_admin_operations_are_audited(monkeypatch):
    module, _tickets, _intake = load_app(monkeypatch)
    admin_headers = headers(
        "ticket:agent",
        "asset:read",
        "asset:write",
        "it-policy:read",
        "it-policy:write",
        "knowledge:read",
        "knowledge:write",
    )
    with TestClient(module.app) as client:
        client.post(
            "/assets",
            headers=admin_headers,
            json={"asset_id": "asset-1", "asset_no": "A-1", "asset_type": "laptop"},
        )
        client.put(
            "/admin/it/policies/it.vpn",
            headers=admin_headers,
            json={"category": "it.vpn", "policy_id": "sla-1"},
        )
        client.post(
            "/knowledge/documents",
            headers=admin_headers,
            json={
                "document": {"document_id": "doc-1", "version": 1, "title": "VPN"},
                "chunks": [{"chunk_id": "c1", "ordinal": 0, "content": "vpn"}],
            },
        )
        audit = module.app.state.runtime.audit
        actions = [event["action"] for event in audit.admin_events]

    assert "asset.create" in actions
    assert "it_policy.upsert" in actions
    assert "knowledge.document.create" in actions
    assert all(event["tenant_id"] == "tenant-a" for event in audit.admin_events)




def test_channel_identity_admin_api_rejects_foreign_asset_and_requires_admin(monkeypatch):
    """Day 3-4：可信渠道身份只允许 security:admin 管理；资产必须归属该渠道用户。"""
    module, _tickets, _intake = load_app(monkeypatch)
    admin = headers(
        "security:admin",
        "ticket:agent",
        "asset:read",
        "asset:write",
    )
    with TestClient(module.app) as client:
        client.post(
            "/assets",
            headers=admin,
            json={"asset_id": "asset-1", "asset_no": "A-1", "asset_type": "laptop", "owner_user_id": "user-1"},
        )

        forbidden = client.get("/admin/channel-identities", headers=headers("ticket:agent"))
        foreign_asset = client.put(
            "/admin/channel-identities/wecom/ext-user-1",
            headers=admin,
            json={"channel": "wecom", "requester_id": "ext-user-1", "departments": ["finance"], "asset_id": "asset-1"},
        )
        own_asset = client.put(
            "/admin/channel-identities/wecom/user-1",
            headers=admin,
            json={"channel": "wecom", "requester_id": "user-1", "departments": ["finance"], "asset_id": "asset-1"},
        )
        listed = client.get("/admin/channel-identities", headers=admin)

    assert forbidden.status_code == 403
    assert foreign_asset.status_code == 404
    assert own_asset.status_code == 200
    assert own_asset.json()["departments"] == ["finance"]
    assert [item["requester_id"] for item in listed.json()["items"]] == ["user-1"]

def test_full_lifecycle_http_regression_vpn(monkeypatch):
    """Day 6：VPN 主链路完整 HTTP 回归（创建→追问→补全→分类→派单→接单→处理→解决→回访→关闭）。"""
    module, tickets, intake = load_app(monkeypatch)
    intake.result = {"__interrupt__": (), "missing_fields": ["impact"]}
    intake.pending = (
        Interrupt(
            id="interrupt-lifecycle-1",
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
        created = client.post(
            "/tickets",
            headers=headers("ticket:customer"),
            json={"ticket_id": "ticket-1", "title": "VPN 无法连接", "description": "错误码 809"},
        )
        assert created.status_code == 201

        intake_resp = client.post(
            "/tickets/ticket-1/intake",
            headers=headers("ticket:customer"),
            json={
                "operation_id": "op-lifecycle-intake",
                "text": "VPN 无法连接，错误码 809",
                "fields": {"title": "VPN", "description": "error 809"},
                "expected_version": 0,
            },
        )
        assert intake_resp.status_code == 200
        assert intake_resp.json()["ticket"]["status"] == "awaiting_customer"

        # 客户补充信息后恢复受理（Fake 图改为返回派单结果）
        intake.result = {"category": "it", "subcategory": "vpn", "dispatch_team_id": "team-it", "priority": "normal"}
        resume_resp = client.post(
            "/tickets/ticket-1/resume",
            headers=headers("ticket:customer"),
            json={
                "operation_id": "op-lifecycle-resume",
                "interrupt_id": "interrupt-lifecycle-1",
                "ticket_id": "ticket-1",
                "actor_type": "customer",
                "actor_id": "user-1",
                "action": "provide_information",
                "expected_version": 2,
                "payload": {"fields": {"impact": "one user"}},
            },
        )
        assert resume_resp.status_code == 200
        assert resume_resp.json()["ticket"]["status"] == "queued"
        current_version = resume_resp.json()["ticket"]["version"]

        def transition(action, actor_type, scopes):
            return client.post(
                "/tickets/ticket-1/transitions",
                headers=headers(*scopes),
                json={
                    "action": action,
                    "expected_version": resume_resp.json()["ticket"]["version"],
                    "actor_type": actor_type,
                    "payload": {},
                },
            )
        # 客服接单 → 开始处理 → 解决
        assigned = transition("assign", "agent", ("ticket:agent",))
        assert assigned.status_code == 200
        assert assigned.json()["status"] == "assigned"
        in_progress = transition("start_work", "agent", ("ticket:agent",))
        assert in_progress.status_code == 200
        assert in_progress.json()["status"] == "in_progress"
        resolved = transition("resolve", "agent", ("ticket:agent",))
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "resolved"

        # 客服发起回访，客户提交满意度
        survey = client.post(
            "/tickets/ticket-1/survey",
            headers=headers("ticket:agent"),
            json={"expires_in_days": 7},
        )
        assert survey.status_code == 201
        survey_id = survey.json()["survey_id"]
        responded = client.post(
            f"/tickets/ticket-1/survey/{survey_id}/response",
            headers=headers("ticket:customer"),
            json={"score": 5, "feedback": "resolved"},
        )
        assert responded.status_code == 200
        assert responded.json()["status"] == "responded"

        # 关闭工单
        closed = transition("close", "agent", ("ticket:agent",))
        assert closed.status_code == 200
        assert closed.json()["status"] == "closed"

        # 收尾：SLA 被补建、回访/消息/审计可达
        assert tickets.operations.sla_calls
        assert tickets.operations.surveys
        final = client.get("/tickets/ticket-1", headers=headers("ticket:customer"))
        assert final.json()["status"] == "closed"
        assert current_version > 0
