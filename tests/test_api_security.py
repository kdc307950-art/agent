import asyncio
from dataclasses import replace
import importlib
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from backend.security import make_tenant_token


def load_app(monkeypatch, rate_limit="60"):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("X_API_KEY", "test-api-key")
    monkeypatch.setenv("TENANT_TOKEN_SECRET", "test-tenant-secret")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("RATE_LIMIT_CAPACITY", rate_limit)
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", rate_limit)
    module = importlib.import_module("backend.app")
    module = importlib.reload(module)

    class EmptyGraph:
        async def astream(self, *_args, **_kwargs):
            if False:
                yield {}

    @asynccontextmanager
    async def fake_runtime_context(_settings):
        yield type("Runtime", (), {"graph": EmptyGraph()})()

    module.runtime_context = fake_runtime_context
    return module


def test_health_is_public_and_agent_starts(monkeypatch):
    module = load_app(monkeypatch)
    with TestClient(module.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "agent_ready": True}


def test_chat_requires_bearer_token(monkeypatch):
    module = load_app(monkeypatch)
    with TestClient(module.app) as client:
        response = client.post("/chat/stream", json={"message": "hello"})

    assert response.status_code == 401


def test_chat_rejects_wrong_bearer_token(monkeypatch):
    module = load_app(monkeypatch)
    with TestClient(module.app) as client:
        response = client.post(
            "/chat/stream",
            headers={"Authorization": "Bearer wrong-key"},
            json={"message": "hello"},
        )

    assert response.status_code == 401


def test_chat_rejects_invalid_input(monkeypatch):
    module = load_app(monkeypatch)
    headers = {"Authorization": "Bearer " + make_tenant_token("tenant-a", "user-1", "test-tenant-secret")}
    with TestClient(module.app) as client:
        response = client.post(
            "/chat/stream",
            headers=headers,
            json={"message": " ", "thread_id": "bad id"},
        )

    assert response.status_code == 422


def test_cors_rejects_unknown_origin(monkeypatch):
    module = load_app(monkeypatch)
    with TestClient(module.app) as client:
        response = client.options(
            "/chat/stream",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_chat_rate_limit_returns_429(monkeypatch):
    module = load_app(monkeypatch, rate_limit="1")
    headers = {"Authorization": "Bearer " + make_tenant_token("tenant-a", "user-1", "test-tenant-secret")}
    with TestClient(module.app) as client:
        module.app.state.agent = None
        first = client.post("/chat/stream", headers=headers, json={"message": "hello"})
        second = client.post("/chat/stream", headers=headers, json={"message": "hello"})

    assert first.status_code == 503
    assert second.status_code == 429
    assert second.headers["Retry-After"].isdigit()
    assert module.app.state.metrics.snapshot()["rate_limit_rejected_total"] == 1


def test_chat_timeout_returns_structured_sse_error(monkeypatch):
    module = load_app(monkeypatch)

    class SlowGraph:
        async def astream(self, *_args, **_kwargs):
            await asyncio.sleep(0.05)
            if False:
                yield {}

    headers = {"Authorization": "Bearer " + make_tenant_token("tenant-a", "user-1", "test-tenant-secret")}
    with TestClient(module.app) as client:
        module.app.state.agent = SlowGraph()
        module.app.state.settings = replace(
            module.app.state.settings,
            agent_run_timeout_seconds=0.01,
        )
        response = client.post(
            "/chat/stream",
            headers=headers,
            json={"message": "hello"},
        )

    assert response.status_code == 200
    assert '"code": "agent_timeout"' in response.text


def test_same_client_thread_is_derived_per_tenant(monkeypatch):
    module = load_app(monkeypatch)
    captured = []

    class CapturingGraph:
        async def astream(self, _inputs, config, **_kwargs):
            captured.append(config["configurable"]["thread_id"])
            if False:
                yield {}

    headers_a = {"Authorization": "Bearer " + make_tenant_token("tenant-a", "user-1", "test-tenant-secret")}
    headers_b = {"Authorization": "Bearer " + make_tenant_token("tenant-b", "user-1", "test-tenant-secret")}
    with TestClient(module.app) as client:
        module.app.state.agent = CapturingGraph()
        client.post("/chat/stream", headers=headers_a, json={"message": "hello", "thread_id": "same"})
        client.post("/chat/stream", headers=headers_b, json={"message": "hello", "thread_id": "same"})

    assert captured == ["tenant-a:user-1:same", "tenant-b:user-1:same"]


def test_token_without_write_scope_is_forbidden(monkeypatch):
    module = load_app(monkeypatch)
    token = make_tenant_token("tenant-a", "user-1", "test-tenant-secret", scopes=("chat:read",))
    with TestClient(module.app) as client:
        response = client.post(
            "/chat/stream",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "hello"},
        )
    assert response.status_code == 403


def test_chat_passes_server_context_and_finishes_audit(monkeypatch):
    module = load_app(monkeypatch)
    captured = {}

    class CapturingAudit:
        async def start_run(self, context, **_kwargs):
            captured["start"] = context

        async def finish_run(self, context, status, **_kwargs):
            captured["finish"] = (context, status)

        async def record_event(self, *_args, **_kwargs):
            return None

    class CapturingGraph:
        async def astream(self, _inputs, config, context, **_kwargs):
            captured["thread_id"] = config["configurable"]["thread_id"]
            captured["context"] = context
            if False:
                yield {}

    token = make_tenant_token("tenant-a", "user-1", "test-tenant-secret")
    with TestClient(module.app) as client:
        module.app.state.agent = CapturingGraph()
        module.app.state.audit = CapturingAudit()
        response = client.post(
            "/chat/stream",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "hello", "thread_id": "same"},
        )

    assert response.status_code == 200
    assert captured["thread_id"] == "tenant-a:user-1:same"
    assert captured["context"].tenant_id == "tenant-a"
    assert captured["context"].user_id == "user-1"
    assert captured["finish"][1] == "completed"


def test_metrics_endpoint_uses_auth_token_when_configured(monkeypatch):
    module = load_app(monkeypatch)
    with TestClient(module.app) as client:
        module.app.state.settings = replace(
            module.app.state.settings,
            metrics_auth_token="metrics-secret",
        )
        unauthorized = client.get("/metrics")
        authorized = client.get("/metrics", headers={"X-Metrics-Token": "metrics-secret"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert "agent_" in authorized.text
