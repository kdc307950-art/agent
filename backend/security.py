"""Authentication, request limits, and configuration helpers for the API."""

from __future__ import annotations

import hmac
import hashlib
import os
import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


bearer_scheme = HTTPBearer(auto_error=False)


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


def authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """Validate the shared local/internal Bearer token."""
    expected = os.getenv("X_API_KEY", "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务未配置 API 鉴权密钥",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要 Bearer 鉴权",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API 鉴权失败",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.authenticated = True
    # Keep only a non-reversible fingerprint in limiter state, never the token itself.
    return hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()[:16]


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
    principal: str = Depends(authenticate),
) -> str:
    limiter = request.app.state.rate_limiter
    client_host = request.client.host if request.client else "unknown"
    retry_after = limiter.check(f"{client_host}:{principal}")
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后重试",
            headers={"Retry-After": str(retry_after)},
        )
    return principal
