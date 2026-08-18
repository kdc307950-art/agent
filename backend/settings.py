from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需环境变量: {name}")
    return value


def database_url_from_env() -> str:
    return _required("DATABASE_URL")


def _int_setting(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数") from exc
    if value < minimum:
        raise RuntimeError(f"环境变量 {name} 必须 >= {minimum}")
    return value


def _choice_setting(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in choices:
        raise RuntimeError(f"环境变量 {name} 必须是: {', '.join(sorted(choices))}")
    return value


@dataclass(frozen=True)
class Settings:
    app_env: str
    deepseek_api_key: str
    llm_base_url: str
    llm_model: str
    auth_mode: str
    tenant_token_secret: str | None
    oidc_issuer_url: str | None
    oidc_audience: str | None
    oidc_jwks_url: str | None
    oidc_tenant_claim: str
    oidc_required_scopes: frozenset[str]
    oidc_clock_skew_seconds: int
    oidc_revocation_mode: str
    database_url: str
    redis_url: str | None
    rate_limit_backend: str
    rate_limit_capacity: int
    rate_limit_refill_per_second: float
    redis_socket_timeout_seconds: float
    redis_fail_mode: str
    agent_run_timeout_seconds: int
    model_retry_attempts: int
    checkpoint_retention_days: int
    auto_setup: bool

    @classmethod
    def from_env(cls) -> "Settings":
        auto_setup = os.getenv("LANGGRAPH_AUTO_SETUP", "false").strip().lower()
        app_env = _choice_setting("APP_ENV", "development", {"development", "test", "staging", "production"})
        auth_mode = _choice_setting("AUTH_MODE", "dev", {"dev", "oidc"})
        tenant_token_secret = os.getenv("TENANT_TOKEN_SECRET", "").strip() or None
        issuer = os.getenv("OIDC_ISSUER_URL", "").strip() or None
        audience = os.getenv("OIDC_AUDIENCE", "").strip() or None
        jwks_url = os.getenv("OIDC_JWKS_URL", "").strip() or None
        if auth_mode == "dev" and not tenant_token_secret:
            raise RuntimeError("AUTH_MODE=dev 必须配置 TENANT_TOKEN_SECRET")
        if auth_mode == "oidc" and (not issuer or not audience):
            raise RuntimeError("AUTH_MODE=oidc 必须配置 OIDC_ISSUER_URL 和 OIDC_AUDIENCE")
        rate_limit_backend = _choice_setting("RATE_LIMIT_BACKEND", "redis", {"memory", "redis"})
        redis_url = os.getenv("REDIS_URL", "").strip() or None
        revocation_mode = _choice_setting("OIDC_REVOCATION_MODE", "none", {"none", "redis"})
        if app_env == "production" and auth_mode != "oidc":
            raise RuntimeError("APP_ENV=production 禁止使用 AUTH_MODE=dev")
        if app_env == "production" and revocation_mode != "redis":
            raise RuntimeError("APP_ENV=production 必须启用 OIDC_REVOCATION_MODE=redis")
        if (rate_limit_backend == "redis" or (auth_mode == "oidc" and revocation_mode == "redis")) and not redis_url:
            raise RuntimeError("当前配置需要 Redis，必须配置 REDIS_URL")
        refill_rate = float(os.getenv("RATE_LIMIT_REFILL_PER_SECOND", "1"))
        if refill_rate <= 0:
            raise RuntimeError("RATE_LIMIT_REFILL_PER_SECOND 必须 > 0")
        required_scopes = frozenset(
            scope.strip() for scope in os.getenv("OIDC_REQUIRED_SCOPES", "chat:write").split(",") if scope.strip()
        )
        return cls(
            app_env=app_env,
            deepseek_api_key=_required("DEEPSEEK_API_KEY"),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip(),
            llm_model=os.getenv("LLM_MODEL", "deepseek-chat").strip(),
            auth_mode=auth_mode,
            tenant_token_secret=tenant_token_secret,
            oidc_issuer_url=issuer,
            oidc_audience=audience,
            oidc_jwks_url=jwks_url,
            oidc_tenant_claim=os.getenv("OIDC_TENANT_CLAIM", "tenant_id").strip() or "tenant_id",
            oidc_required_scopes=required_scopes,
            oidc_clock_skew_seconds=_int_setting("OIDC_CLOCK_SKEW_SECONDS", 60, 0),
            oidc_revocation_mode=revocation_mode,
            database_url=database_url_from_env(),
            redis_url=redis_url,
            rate_limit_backend=rate_limit_backend,
            rate_limit_capacity=_int_setting("RATE_LIMIT_CAPACITY", 60, 1),
            rate_limit_refill_per_second=refill_rate,
            redis_socket_timeout_seconds=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "1")),
            redis_fail_mode=_choice_setting("REDIS_FAIL_MODE", "closed", {"open", "closed"}),
            agent_run_timeout_seconds=_int_setting("AGENT_RUN_TIMEOUT_SECONDS", 60, 1),
            model_retry_attempts=_int_setting("MODEL_RETRY_ATTEMPTS", 2, 0),
            checkpoint_retention_days=_int_setting("CHECKPOINT_RETENTION_DAYS", 30, 1),
            auto_setup=auto_setup in {"1", "true", "yes"},
        )
