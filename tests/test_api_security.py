import importlib

from fastapi.testclient import TestClient


def load_app(monkeypatch, rate_limit="60"):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("X_API_KEY", "test-api-key")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", rate_limit)
    module = importlib.import_module("backend.app")
    module = importlib.reload(module)
    class EmptyAgent:
        def stream(self, *_args, **_kwargs):
            return []

    module.build_agent = lambda: EmptyAgent()
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
    headers = {"Authorization": "Bearer test-api-key"}
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
    headers = {"Authorization": "Bearer test-api-key"}
    with TestClient(module.app) as client:
        module.app.state.agent = None
        first = client.post("/chat/stream", headers=headers, json={"message": "hello"})
        second = client.post("/chat/stream", headers=headers, json={"message": "hello"})

    assert first.status_code == 503
    assert second.status_code == 429
    assert second.headers["Retry-After"].isdigit()
