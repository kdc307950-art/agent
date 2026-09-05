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
    monkeypatch.setenv("VPN_ADAPTER_MODE", "real")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("RATE_LIMIT_BACKEND", "memory", "RATE_LIMIT_BACKEND"),
        ("REDIS_FAIL_MODE", "open", "REDIS_FAIL_MODE"),
        ("LANGGRAPH_AUTO_SETUP", "true", "AUTO_SETUP"),
        ("VPN_ADAPTER_MODE", "mock", "VPN_ADAPTER_MODE"),
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


def test_channel_webhook_configuration_must_be_complete(monkeypatch):
    _base_production(monkeypatch)
    monkeypatch.setenv("WECOM_TENANT_ID", "tenant-a")
    # 清空其余企微变量（本地 .env 可能已配置完整凭据，会污染"部分配置"断言）。
    for name in ("WECOM_TOKEN", "WECOM_ENCODING_AES_KEY", "WECOM_CORP_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="WECOM_TENANT_ID"):
        Settings.from_env()

    _base_production(monkeypatch)
    monkeypatch.delenv("WECOM_TENANT_ID", raising=False)
    monkeypatch.delenv("WECOM_TOKEN", raising=False)
    monkeypatch.delenv("WECOM_ENCODING_AES_KEY", raising=False)
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.setenv("DINGTALK_TENANT_ID", "tenant-a")
    monkeypatch.delenv("DINGTALK_APP_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="DINGTALK_TENANT_ID"):
        Settings.from_env()


def test_production_budget_requires_nonzero_model_price(monkeypatch):
    _base_production(monkeypatch)
    monkeypatch.setenv("TENANT_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MODEL_INPUT_COST_PER_1K_USD", "0")
    monkeypatch.setenv("MODEL_OUTPUT_COST_PER_1K_USD", "0")
    with pytest.raises(RuntimeError, match="模型输入或输出价格"):
        Settings.from_env()


def test_fortimanager_gateway_requires_complete_explicit_configuration(monkeypatch):
    _base_production(monkeypatch)
    monkeypatch.setenv("VPN_COMMAND_GATEWAY_MODE", "fortimanager")
    monkeypatch.delenv("VPN_FMG_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="VPN_FMG_BASE_URL"):
        Settings.from_env()

    _base_production(monkeypatch)
    monkeypatch.setenv("VPN_COMMAND_GATEWAY_MODE", "fortimanager")
    monkeypatch.setenv("VPN_FMG_BASE_URL", "https://fmg.example.com")
    monkeypatch.setenv("VPN_FMG_API_TOKEN", "test-token")
    monkeypatch.setenv("VPN_FMG_TENANT_TARGETS_JSON", '{"tenant-a": {}}')
    assert Settings.from_env().vpn_command_gateway_mode == "fortimanager"
