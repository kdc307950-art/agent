"""配置模块 —— 集中读取并校验所有环境变量（失败即报错，防止带病启动）。

Settings 是不可变 dataclass，from_env() 做类型转换 + 生产环境强约束：
    - APP_ENV=production 时强制 OIDC 鉴权、Redis 限流/撤销、关闭 auto_setup 等
    - 所有 *_setting 辅助函数负责解析单类环境变量并给出清晰报错
"""

from __future__ import annotations

import os
import re
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


def _float_setting(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是数字") from exc
    if value < minimum:
        raise RuntimeError(f"环境变量 {name} 必须 >= {minimum}")
    return value


def _choice_setting(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in choices:
        raise RuntimeError(f"环境变量 {name} 必须是: {', '.join(sorted(choices))}")
    return value


def _bool_setting(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"环境变量 {name} 必须是 true/false")


def _tool_allowlist_setting() -> dict[str, frozenset[str]] | None:
    """Parse tenant=tool1,tool2;tenant2=tool1 or leave unset for all tools."""
    raw = os.getenv("TOOL_TENANT_ALLOWLIST", "").strip()
    if not raw:
        return None
    allowlist: dict[str, frozenset[str]] = {}
    for entry in raw.split(";"):
        tenant, separator, tools = entry.partition("=")
        tenant = tenant.strip()
        if not separator or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tenant):
            raise RuntimeError("TOOL_TENANT_ALLOWLIST 必须使用 tenant=tool1,tool2 格式")
        if tenant in allowlist:
            raise RuntimeError(f"TOOL_TENANT_ALLOWLIST 不能重复配置租户: {tenant}")
        tool_names = frozenset(tool.strip() for tool in tools.split(",") if tool.strip())
        if not tool_names:
            raise RuntimeError("TOOL_TENANT_ALLOWLIST 不能为租户配置空工具列表")
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tool) for tool in tool_names):
            raise RuntimeError("TOOL_TENANT_ALLOWLIST 中包含非法工具名")
        allowlist[tenant] = tool_names
    return allowlist


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
    oidc_jwks_cache_seconds: int
    oidc_require_jti: bool
    oidc_max_token_age_seconds: int
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
    tool_retry_attempts: int
    tool_tenant_allowlist: dict[str, frozenset[str]] | None
    audit_write_timeout_seconds: float
    metrics_enabled: bool
    metrics_auth_token: str | None
    checkpoint_retention_days: int
    audit_retention_days: int
    audit_retention_batch_size: int
    audit_retention_max_runtime_seconds: float
    audit_retention_enabled: bool
    model_input_cost_per_1k_usd: float
    model_output_cost_per_1k_usd: float
    tenant_daily_budget_usd: float
    auto_setup: bool
    agent_graph_mode: str
    agent_workflow_path: str | None

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
        redis_fail_mode = _choice_setting("REDIS_FAIL_MODE", "closed", {"open", "closed"})
        auto_setup_enabled = auto_setup in {"1", "true", "yes"}
        if app_env == "production" and auth_mode != "oidc":
            raise RuntimeError("APP_ENV=production 禁止使用 AUTH_MODE=dev")
        if app_env == "production" and revocation_mode != "redis":
            raise RuntimeError("APP_ENV=production 必须启用 OIDC_REVOCATION_MODE=redis")
        if app_env == "production" and rate_limit_backend != "redis":
            raise RuntimeError("APP_ENV=production 必须使用 RATE_LIMIT_BACKEND=redis")
        if app_env == "production" and redis_fail_mode != "closed":
            raise RuntimeError("APP_ENV=production 必须使用 REDIS_FAIL_MODE=closed")
        if app_env == "production" and auto_setup_enabled:
            raise RuntimeError("APP_ENV=production 禁止 LANGGRAPH_AUTO_SETUP=true")
        if app_env == "production" and not os.getenv("CORS_ALLOWED_ORIGINS", "").strip():
            raise RuntimeError("APP_ENV=production 必须显式配置 CORS_ALLOWED_ORIGINS")
        oidc_require_jti = _bool_setting("OIDC_REQUIRE_JTI", app_env == "production")
        oidc_max_token_age_seconds = _int_setting("OIDC_MAX_TOKEN_AGE_SECONDS", 3600, 0)
        if app_env == "production" and not oidc_require_jti:
            raise RuntimeError("APP_ENV=production 必须要求 OIDC token 携带 jti")
        if app_env == "production" and oidc_max_token_age_seconds <= 0:
            raise RuntimeError("APP_ENV=production 必须配置 OIDC_MAX_TOKEN_AGE_SECONDS")
        metrics_enabled = _bool_setting("METRICS_ENABLED", True)
        metrics_auth_token = os.getenv("METRICS_AUTH_TOKEN", "").strip() or None
        if app_env == "production" and metrics_enabled and not metrics_auth_token:
            raise RuntimeError("APP_ENV=production 启用 metrics 时必须配置 METRICS_AUTH_TOKEN")
        if (rate_limit_backend == "redis" or (auth_mode == "oidc" and revocation_mode == "redis")) and not redis_url:
            raise RuntimeError("当前配置需要 Redis，必须配置 REDIS_URL")
        refill_rate = float(os.getenv("RATE_LIMIT_REFILL_PER_SECOND", "1"))
        if refill_rate <= 0:
            raise RuntimeError("RATE_LIMIT_REFILL_PER_SECOND 必须 > 0")
        required_scopes = frozenset(
            scope.strip() for scope in os.getenv("OIDC_REQUIRED_SCOPES", "chat:write").split(",") if scope.strip()
        )
        if auth_mode == "oidc" and not required_scopes:
            raise RuntimeError("AUTH_MODE=oidc 必须至少配置一个 OIDC_REQUIRED_SCOPES")
        tenant_daily_budget_usd = _float_setting("TENANT_DAILY_BUDGET_USD", 0.0, 0.0)
        if tenant_daily_budget_usd > 0 and not redis_url:
            raise RuntimeError("启用 TENANT_DAILY_BUDGET_USD 时必须配置 REDIS_URL")
        input_cost = _float_setting("MODEL_INPUT_COST_PER_1K_USD", 0.0, 0.0)
        output_cost = _float_setting("MODEL_OUTPUT_COST_PER_1K_USD", 0.0, 0.0)
        if app_env == "production" and tenant_daily_budget_usd > 0 and input_cost <= 0 and output_cost <= 0:
            raise RuntimeError("生产启用租户预算时必须配置模型输入或输出价格")
        # 图形态：single=单 Agent（默认，向后兼容）；workflow=按 JSON 编排图
        agent_graph_mode = _choice_setting("AGENT_GRAPH_MODE", "single", {"single", "workflow"})
        agent_workflow_path = os.getenv("AGENT_WORKFLOW_PATH", "").strip() or None
        if agent_graph_mode == "workflow" and not agent_workflow_path:
            raise RuntimeError("AGENT_GRAPH_MODE=workflow 必须配置 AGENT_WORKFLOW_PATH")
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
            oidc_jwks_cache_seconds=_int_setting("OIDC_JWKS_CACHE_SECONDS", 300, 1),
            oidc_require_jti=oidc_require_jti,
            oidc_max_token_age_seconds=oidc_max_token_age_seconds,
            oidc_revocation_mode=revocation_mode,
            database_url=database_url_from_env(),
            redis_url=redis_url,
            rate_limit_backend=rate_limit_backend,
            rate_limit_capacity=_int_setting("RATE_LIMIT_CAPACITY", 60, 1),
            rate_limit_refill_per_second=refill_rate,
            redis_socket_timeout_seconds=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "1")),
            redis_fail_mode=redis_fail_mode,
            agent_run_timeout_seconds=_int_setting("AGENT_RUN_TIMEOUT_SECONDS", 60, 1),
            model_retry_attempts=_int_setting("MODEL_RETRY_ATTEMPTS", 2, 0),
            tool_retry_attempts=_int_setting("TOOL_RETRY_ATTEMPTS", 1, 0),
            tool_tenant_allowlist=_tool_allowlist_setting(),
            audit_write_timeout_seconds=_float_setting("AUDIT_WRITE_TIMEOUT_SECONDS", 2.0, 0.1),
            metrics_enabled=metrics_enabled,
            metrics_auth_token=metrics_auth_token,
            checkpoint_retention_days=_int_setting("CHECKPOINT_RETENTION_DAYS", 30, 1),
            audit_retention_days=_int_setting("AUDIT_RETENTION_DAYS", 30, 1),
            audit_retention_batch_size=_int_setting("AUDIT_RETENTION_BATCH_SIZE", 1000, 1),
            audit_retention_max_runtime_seconds=_float_setting(
                "AUDIT_RETENTION_MAX_RUNTIME_SECONDS", 60.0, 0.1
            ),
            audit_retention_enabled=_bool_setting("AUDIT_RETENTION_ENABLED", False),
            model_input_cost_per_1k_usd=input_cost,
            model_output_cost_per_1k_usd=output_cost,
            tenant_daily_budget_usd=tenant_daily_budget_usd,
            auto_setup=auto_setup_enabled,
            agent_graph_mode=agent_graph_mode,
            agent_workflow_path=agent_workflow_path,
        )
