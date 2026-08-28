"""认证与限流 —— API 的鉴权、速率限制、配置辅助。

提供：
    - Principal:         已认证主体（租户 + 用户 + scopes）
    - OIDCVerifier:      校验 OIDC JWT（JWKS 缓存、jti、过期撤销）
    - make_tenant_token / authenticate: 开发模式 token 签发与解析
    - rate_limit_dependency: FastAPI 依赖形式的限流（内存/Redis）
    - cors_origins:      从环境变量读取 CORS 白名单
"""

from __future__ import annotations

import hmac
import hashlib
import base64
import json
import os
import time
import logging
from dataclasses import dataclass

import httpx
import jwt
from jwt import InvalidTokenError

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .rate_limit import InMemoryRateLimiter


bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger("langgraph.security")

_TOKEN_VERSION = "v1"
_IDENTIFIER = r"^[A-Za-z0-9_.-]{1,64}$"


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    scopes: frozenset[str]

    @property
    def limiter_key(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"


class OIDCVerifier:
    """Validate OIDC JWTs against a cached JWKS document."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None,
        tenant_claim: str,
        clock_skew_seconds: int,
        required_scopes: frozenset[str],
        revocation_store=None,
        cache_seconds: int = 300,
        require_jti: bool = False,
        max_token_age_seconds: int = 0,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_url = jwks_url
        self.discovery_url = f"{self.issuer}/.well-known/openid-configuration"
        self.tenant_claim = tenant_claim
        self.clock_skew_seconds = clock_skew_seconds
        self.required_scopes = required_scopes
        self.revocation_store = revocation_store
        self.cache_seconds = cache_seconds
        self.require_jti = require_jti
        self.max_token_age_seconds = max_token_age_seconds
        self._jwks: dict[str, dict] = {}
        self._jwks_expires_at = 0.0
        self._client = httpx.AsyncClient(timeout=5.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_ready(self) -> None:
        """Ensure the configured issuer/JWKS endpoint is reachable and valid."""
        await self._load_jwks()
        if not self._jwks:
            raise RuntimeError("OIDC JWKS 为空")

    async def _load_jwks(self, *, force: bool = False) -> None:
        if not force and self._jwks and self._jwks_expires_at > time.monotonic():
            return
        if not self.jwks_url:
            discovery = await self._client.get(self.discovery_url)
            discovery.raise_for_status()
            self.jwks_url = str(discovery.json()["jwks_uri"])
        response = await self._client.get(self.jwks_url)
        response.raise_for_status()
        keys = response.json().get("keys", [])
        self._jwks = {str(key["kid"]): key for key in keys if key.get("kid")}
        self._jwks_expires_at = time.monotonic() + self.cache_seconds

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            kid = header.get("kid")
            if algorithm not in {"RS256", "ES256"} or not kid:
                raise ValueError("OIDC token header 无效")
            await self._load_jwks()
            key_data = self._jwks.get(str(kid))
            if key_data is None:
                await self._load_jwks(force=True)
                key_data = self._jwks.get(str(kid))
            if key_data is None:
                raise ValueError("OIDC token 的 kid 不存在")
            key = jwt.algorithms.get_default_algorithms()[algorithm].from_jwk(json.dumps(key_data))
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.clock_skew_seconds,
                options={"require": ["exp", "sub"]},
            )
        except (InvalidTokenError, KeyError, TypeError, ValueError, httpx.HTTPError) as exc:
            raise ValueError("OIDC token 校验失败") from exc

        jti = str(claims.get("jti", ""))
        now = int(time.time())
        issued_at = claims.get("iat")
        if issued_at is not None:
            try:
                issued_at = int(issued_at)
            except (TypeError, ValueError) as exc:
                raise ValueError("OIDC token 的 iat 无效") from exc
            if issued_at > now + self.clock_skew_seconds:
                raise ValueError("OIDC token 尚未签发")
            if self.max_token_age_seconds and now - issued_at > self.max_token_age_seconds + self.clock_skew_seconds:
                raise ValueError("OIDC token 超过允许年龄")
        elif self.max_token_age_seconds:
            raise ValueError("OIDC token 缺少 iat")
        if self.require_jti and not jti:
            raise ValueError("OIDC token 缺少 jti")
        if self.revocation_store is not None and jti:
            try:
                revoked = await self.revocation_store.is_revoked(jti)
            except Exception as exc:
                raise RuntimeError("OIDC 撤销服务不可用") from exc
            if revoked:
                raise ValueError("OIDC token 已撤销")
        tenant_id = str(claims.get(self.tenant_claim, ""))
        user_id = str(claims.get("sub", ""))
        if not _valid_identifier(tenant_id) or not _valid_identifier(user_id):
            raise ValueError("OIDC token 缺少有效租户身份")
        raw_scopes = claims.get("scope", claims.get("scp", claims.get("roles", [])))
        scopes = frozenset(raw_scopes.split() if isinstance(raw_scopes, str) else (str(item) for item in raw_scopes))
        if not self.required_scopes.issubset(scopes):
            raise PermissionError("OIDC token 缺少所需 scope")
        return Principal(tenant_id=tenant_id, user_id=user_id, scopes=scopes)


def _valid_identifier(value: str) -> bool:
    import re

    return bool(re.fullmatch(_IDENTIFIER, value))


def required_setting(name: str) -> str:
    """Return a non-empty environment setting or fail startup."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需环境变量: {name}")
    return value


def cors_origins() -> list[str]:
    """Read a comma-separated, explicit CORS allowlist."""
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
    if os.getenv("APP_ENV", "development").strip().lower() == "production" and not raw:
        raise RuntimeError("APP_ENV=production 必须显式配置 CORS_ALLOWED_ORIGINS")
    if not raw:
        raw = "http://127.0.0.1:5173,http://localhost:5173,http://localhost:3000"
    origins = [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]
    if not origins:
        raise RuntimeError("CORS_ALLOWED_ORIGINS 不能是空值")
    if "*" in origins:
        raise RuntimeError("CORS_ALLOWED_ORIGINS 不允许使用通配符 *")
    return origins


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


DEV_SCOPE_PROFILES: dict[str, tuple[str, ...]] = {
    "chat": ("chat:read", "chat:write", "chat:approve"),
    "helpdesk-agent": (
        "chat:read",
        "chat:write",
        "ticket:customer",
        "ticket:agent",
        # 客服可查看资产、IT 策略与知识，读写权限交给 IT 管理员角色
        "asset:read",
        "it-policy:read",
        "knowledge:read",
    ),
    "helpdesk-customer": ("ticket:customer", "asset:read"),
    "helpdesk-channel": ("ticket:channel",),
    "helpdesk-approver": ("ticket:agent", "ticket:approve"),
    "helpdesk-it-admin": (
        "ticket:agent",
        "asset:read",
        "asset:write",
        "it-policy:read",
        "it-policy:write",
        "knowledge:read",
        "knowledge:write",
    ),
}


def scopes_for_dev_role(role: str) -> tuple[str, ...]:
    try:
        return DEV_SCOPE_PROFILES[role]
    except KeyError as exc:
        raise ValueError(f"未知开发令牌角色: {role}") from exc


def make_tenant_token(
    tenant_id: str,
    user_id: str,
    secret: str,
    *,
    # chat:approve 允许恢复被 human_approval 挂起的运行；本地/集成 token 默认下发，
    # 生产走 OIDC 时应由 IdP 单独授予，不要和 chat:write 绑定
    scopes: tuple[str, ...] = ("chat:read", "chat:write", "chat:approve"),
    ttl_seconds: int = 3600,
    now: int | None = None,
) -> str:
    """Create an internal signed token for local/integration use.

    Production deployments should replace this issuer with OIDC/JWT validation.
    """
    import re

    if not re.fullmatch(_IDENTIFIER, tenant_id) or not re.fullmatch(_IDENTIFIER, user_id):
        raise ValueError("租户或用户标识包含非法字符")
    issued_at = int(time.time() if now is None else now)
    payload = {"tenant_id": tenant_id, "user_id": user_id, "scopes": list(scopes), "exp": issued_at + ttl_seconds}
    encoded = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    body = f"{_TOKEN_VERSION}.{encoded}"
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(signature)}"


def _parse_tenant_token(token: str, secret: str) -> Principal:
    import re

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        raise ValueError("令牌格式无效")
    body = ".".join(parts[:2])
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    actual = _b64url_decode(parts[2])
    if not hmac.compare_digest(actual, expected):
        raise ValueError("令牌签名无效")
    payload = json.loads(_b64url_decode(parts[1]))
    if int(payload.get("exp", 0)) <= int(time.time()):
        raise ValueError("令牌已过期")
    tenant_id = str(payload["tenant_id"])
    user_id = str(payload["user_id"])
    if not re.fullmatch(_IDENTIFIER, tenant_id) or not re.fullmatch(_IDENTIFIER, user_id):
        raise ValueError("令牌身份无效")
    scopes = frozenset(str(scope) for scope in payload.get("scopes", []))
    return Principal(tenant_id=tenant_id, user_id=user_id, scopes=scopes)


async def authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    """Validate a dev token or OIDC token according to startup mode."""
    settings = getattr(request.app.state, "settings", None)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要 Bearer 鉴权",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        if getattr(settings, "auth_mode", "dev") == "oidc":
            principal = await request.app.state.auth_verifier.verify(credentials.credentials)
        else:
            secret = getattr(settings, "tenant_token_secret", "") or os.getenv("TENANT_TOKEN_SECRET", "").strip()
            if not secret:
                raise RuntimeError("服务未配置租户令牌密钥")
            principal = _parse_tenant_token(credentials.credentials, secret)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API 鉴权失败",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.authenticated = True
    request.state.principal = principal
    return principal


async def enforce_rate_limit(request: Request, principal_key: str, route_key: str) -> None:
    limiter = request.app.state.rate_limiter
    try:
        retry_after = await limiter.check(principal_key, route_key)
    except Exception as exc:
        request.app.state.metrics.increment("rate_limit_errors_total")
        logger.exception("rate limiter failed")
        if request.app.state.settings.redis_fail_mode == "open":
            retry_after = await request.app.state.memory_rate_limiter.check(
                principal_key,
                route_key,
            )
        else:
            raise HTTPException(status_code=503, detail="限流服务暂时不可用") from exc
    if retry_after is not None:
        request.app.state.metrics.increment("rate_limit_rejected_total")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后重试",
            headers={"Retry-After": str(retry_after)},
        )
    request.app.state.metrics.increment("rate_limit_allowed_total")


async def rate_limit_dependency(
    request: Request,
    principal: Principal = Depends(authenticate),
) -> Principal:
    route = getattr(getattr(request, "scope", {}).get("route"), "path", None)
    route_key = route if isinstance(route, str) and route else request.url.path
    await enforce_rate_limit(request, principal.limiter_key, route_key)
    return principal
