"""生产就绪检查（readiness probe）。

`/livez` 检查进程存活（操作系统级），`/readyz` 检查依赖是否可用。
负载均衡器应只把流量转发给 `readyz=200` 的实例，依赖不可用时应该摘流量（容器 HEALTHCHECK 探 /livez）
而不是重启进程。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .schema import check_schema_ready
from .worker_metrics import WORKER_TYPES


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


async def _probe_worker_heartbeats(database_url: str, ttl_seconds: float) -> dict[str, str]:
    """每类 worker（inbound/outbox/sla/recovery）至少一个心跳在 TTL 内 => ok。"""
    try:
        async with await AsyncConnection.connect(database_url) as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT worker_type,
                           count(*) FILTER (WHERE last_beat_at > now() - (%s * interval '1 second')) AS fresh
                    FROM worker_heartbeats
                    GROUP BY worker_type
                    """,
                    (ttl_seconds,),
                )
                rows = {row["worker_type"]: row["fresh"] for row in await cursor.fetchall()}
    except Exception:
        return {worker_type: "failed" for worker_type in WORKER_TYPES}
    return {
        worker_type: "ok" if rows.get(worker_type, 0) >= 1 else "missing"
        for worker_type in WORKER_TYPES
    }


async def _probe_outbox(database_url: str) -> dict[str, Any]:
    """outbox 积压（待投递 pending）与死信（dead）数量。"""
    try:
        async with await AsyncConnection.connect(database_url) as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT
                        count(*) FILTER (WHERE status = 'pending' AND available_at <= now()) AS pending,
                        count(*) FILTER (WHERE status = 'dead') AS dead
                    FROM outbox_events
                    """)
                return dict(await cursor.fetchone() or {"pending": 0, "dead": 0})
    except Exception:
        return {"pending": -1, "dead": -1}


async def probe_dependencies(request: Any) -> ReadinessResult:
    """按当前配置模式探测必要依赖。

    始终检查：agent 对象初始化、Postgres schema 版本。
    按配置检查：限流模式为 Redis 或 OIDC 撤销用 Redis 时检查 Redis；
    认证模式为 OIDC 时检查 JWKS 刮取与缓存。
    可选检查（READINESS_CHECK_WORKERS=true）：Worker 心跳、Outbox 积压与死信。
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
        checks["redis"] = await _probe_redis(
            getattr(request.app.state, "redis_client", None), timeout
        )

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

    if getattr(settings, "readiness_check_workers", False):
        ttl_seconds = float(getattr(settings, "worker_heartbeat_ttl_seconds", 90))
        heartbeats = await _probe_worker_heartbeats(settings.database_url, ttl_seconds)
        checks.update(
            {f"worker_{worker_type}": status for worker_type, status in heartbeats.items()}
        )
        backlog = await _probe_outbox(settings.database_url)
        checks["outbox_backlog"] = (
            "ok" if backlog["pending"] >= 0 and backlog["pending"] < 100 else "degraded"
        )
        checks["outbox_dead"] = "ok" if backlog["dead"] == 0 else "failed"

    return ReadinessResult(ok=all(value == "ok" for value in checks.values()), checks=checks)
