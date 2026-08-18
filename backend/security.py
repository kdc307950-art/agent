"""Authentication, request limits, and configuration helpers for the API."""

from __future__ import annotations

import hmac
import hashlib
import base64
import json
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


bearer_scheme = HTTPBearer(auto_error=False)

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


def required_setting(name: str) -> str:
    """Return a non-empty environment setting or fail startup."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需环境变量: {name}")
    return value


def cors_origins() -> list[str]:
    """Read a comma-separated, explicit CORS allowlist."""
    raw = os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173,http://localhost:3000",
    )
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


def make_tenant_token(
    tenant_id: str,
    user_id: str,
    secret: str,
    *,
    scopes: tuple[str, ...] = ("chat:read", "chat:write"),
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


def authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    """Validate a tenant-scoped signed token."""
    settings = getattr(request.app.state, "settings", None)
    secret = getattr(settings, "tenant_token_secret", "") or os.getenv("TENANT_TOKEN_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="服务未配置租户令牌密钥")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要 Bearer 鉴权",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        principal = _parse_tenant_token(credentials.credentials, secret)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API 鉴权失败",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.authenticated = True
    request.state.principal = principal
    return principal


class InMemoryRateLimiter:
    """A small single-process sliding-window limiter for local deployments."""

    def __init__(self, limit: int = 60, window_seconds: int = 60) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("限流参数必须为正数")
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str) -> int | None:
        """Return retry seconds when blocked, otherwise record the request."""
        now = time.monotonic()
        entries = self._requests[key]
        cutoff = now - self.window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()
        if len(entries) >= self.limit:
            return max(1, int(entries[0] + self.window_seconds - now + 0.999))
        entries.append(now)
        return None


def rate_limit_dependency(
    request: Request,
    principal: Principal = Depends(authenticate),
) -> Principal:
    if "chat:write" not in principal.scopes:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少 chat:write 权限")
    limiter = request.app.state.rate_limiter
    client_host = request.client.host if request.client else "unknown"
    retry_after = limiter.check(f"{client_host}:{principal.limiter_key}")
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后重试",
            headers={"Retry-After": str(retry_after)},
        )
    return principal
