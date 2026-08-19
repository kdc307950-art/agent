import pytest

from backend.settings import Settings


def _base_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://issuer.example")
    monkeypatch.setenv("OIDC_AUDIENCE", "agent-api")
    monkeypatch.setenv("OIDC_REQUIRED_SCOPES", "chat:write")
    monkeypatch.setenv("OIDC_REVOCATION_MODE", "redis")
    monkeypatch.setenv("OIDC_REQUIRE_JTI", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("REDIS_FAIL_MODE", "closed")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example")
    monkeypatch.setenv("METRICS_AUTH_TOKEN", "metrics-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("RATE_LIMIT_BACKEND", "memory", "RATE_LIMIT_BACKEND"),
        ("REDIS_FAIL_MODE", "open", "REDIS_FAIL_MODE"),
        ("LANGGRAPH_AUTO_SETUP", "true", "AUTO_SETUP"),
    ],
)
def test_production_rejects_unsafe_runtime_modes(monkeypatch, name, value, message):
    _base_production(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        Settings.from_env()


def test_oidc_rejects_empty_scope_and_production_requires_cors(monkeypatch):
    _base_production(monkeypatch)
    monkeypatch.setenv("OIDC_REQUIRED_SCOPES", "")
    with pytest.raises(RuntimeError, match="OIDC_REQUIRED_SCOPES"):
        Settings.from_env()

    _base_production(monkeypatch)
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS")
    with pytest.raises(RuntimeError, match="CORS"):
        Settings.from_env()


def test_production_budget_requires_nonzero_model_price(monkeypatch):
    _base_production(monkeypatch)
    monkeypatch.setenv("TENANT_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MODEL_INPUT_COST_PER_1K_USD", "0")
    monkeypatch.setenv("MODEL_OUTPUT_COST_PER_1K_USD", "0")
    with pytest.raises(RuntimeError, match="模型输入或输出价格"):
        Settings.from_env()
