"""生产就绪检查（readiness probe）。

`/livez` 检查进程存活（操作系统级），`/readyz` 检查依赖是否可用。
负载均衡器应只把流量转发给 `readyz=200` 的实例，依赖不可用时应该摘流量（容器 HEALTHCHECK 探 /livez）
而不是重启进程。
"""

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
    """连接 Postgres 并验证 schema 版本是否与本进程兼容。"""
    try:
        async with await asyncio.wait_for(
            AsyncConnection.connect(database_url), timeout=timeout_seconds
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")  # 验证连通性
                await cursor.fetchone()
            await check_schema_ready(connection)  # 验证 schema 版本一致（migration 无遗漏）
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
    """按当前配置模式探测必要依赖。

    始终检查：agent 对象初始化、Postgres schema 版本。
    按配置检查：限流模式为 Redis 或 OIDC 撤销用 Redis 时检查 Redis；
    认证模式为 OIDC 时检查 JWKS 刮取与缓存。
    """

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
                # 触发 JWKS 刮取和验证器初始化，失败表示无法联系 OIDC issuer 或本地 config 有误
                await asyncio.wait_for(verifier.check_ready(), timeout=timeout)
                checks["oidc"] = "ok"
            except Exception:
                checks["oidc"] = "failed"

    return ReadinessResult(ok=all(value == "ok" for value in checks.values()), checks=checks)
