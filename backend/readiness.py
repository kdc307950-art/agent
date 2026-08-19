"""Dependency probes used by the Kubernetes-style readiness endpoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis
from psycopg import AsyncConnection

from .schema import check_schema_ready


@dataclass(frozen=True)
class ReadinessResult:
    ok: bool
    checks: dict[str, str]


async def _probe_postgres(database_url: str, timeout_seconds: float) -> str:
    try:
        async with await asyncio.wait_for(
            AsyncConnection.connect(database_url), timeout=timeout_seconds
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
            await check_schema_ready(connection)
        return "ok"
    except Exception:
        return "failed"


async def _probe_redis(client: Any, timeout_seconds: float) -> str:
    if client is None:
        return "not_configured"
    try:
        await asyncio.wait_for(client.ping(), timeout=timeout_seconds)
        return "ok"
    except Exception:
        return "failed"


async def probe_dependencies(request: Any) -> ReadinessResult:
    """Probe only the dependencies required by the current application mode."""

    settings = request.app.state.settings
    checks: dict[str, str] = {
        "agent": "ok" if request.app.state.agent is not None else "failed",
    }
    timeout = min(float(settings.redis_socket_timeout_seconds), 2.0)
    checks["postgres"] = await _probe_postgres(settings.database_url, timeout)

    redis_required = settings.rate_limit_backend == "redis" or (
        settings.auth_mode == "oidc" and settings.oidc_revocation_mode == "redis"
    )
    if redis_required:
        checks["redis"] = await _probe_redis(getattr(request.app.state, "redis_client", None), timeout)

    if settings.auth_mode == "oidc":
        verifier = getattr(request.app.state, "auth_verifier", None)
        if verifier is None:
            checks["oidc"] = "failed"
        else:
            try:
                await asyncio.wait_for(verifier.check_ready(), timeout=timeout)
                checks["oidc"] = "ok"
            except Exception:
                checks["oidc"] = "failed"

    return ReadinessResult(ok=all(value == "ok" for value in checks.values()), checks=checks)
