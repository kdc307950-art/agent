"""审计记录的有界清理 —— 定时删除过期审计行。

设计：清理命令刻意与 API 进程分离，用单个 PostgreSQL advisory lock
保证多调度器/多 worker 并发安全；短事务 + 批大小限制控制锁时长与写放大。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from .metrics import RuntimeMetrics
from .settings import database_url_from_env

logger = logging.getLogger("langgraph.retention")

# pg_try_advisory_lock(bigint) accepts a signed 64-bit integer.  Deriving the
# value from a stable name avoids magic numbers while remaining deterministic.
RETENTION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"langgraph:agent-audit-retention").digest()[:8],
    byteorder="big",
    signed=True,
)


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数") from exc
    if value < minimum:
        raise RuntimeError(f"环境变量 {name} 必须 >= {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是数字") from exc
    if value < minimum:
        raise RuntimeError(f"环境变量 {name} 必须 >= {minimum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"环境变量 {name} 必须是 true/false")


@dataclass(frozen=True)
class RetentionConfig:
    retention_days: int = 30
    batch_size: int = 1000
    max_runtime_seconds: float = 60.0
    enabled: bool = False
    lock_key: int = RETENTION_LOCK_KEY

    def __post_init__(self) -> None:
        if self.retention_days < 1:
            raise ValueError("retention_days must be >= 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if self.max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be > 0")

    @classmethod
    def from_env(cls) -> RetentionConfig:
        return cls(
            retention_days=_env_int("AUDIT_RETENTION_DAYS", 30, 1),
            batch_size=_env_int("AUDIT_RETENTION_BATCH_SIZE", 1000, 1),
            max_runtime_seconds=_env_float("AUDIT_RETENTION_MAX_RUNTIME_SECONDS", 60.0, 0.1),
            enabled=_env_bool("AUDIT_RETENTION_ENABLED", False),
        )

    @classmethod
    def from_settings(cls, settings: Any) -> RetentionConfig:
        """Build a retention config from the application's Settings object."""
        return cls(
            retention_days=settings.audit_retention_days,
            batch_size=settings.audit_retention_batch_size,
            max_runtime_seconds=settings.audit_retention_max_runtime_seconds,
            enabled=settings.audit_retention_enabled,
        )


@dataclass(frozen=True)
class RetentionResult:
    cutoff: datetime
    dry_run: bool
    lock_acquired: bool
    eligible_runs: int = 0
    deleted_runs: int = 0
    deleted_events: int = 0
    batches: int = 0
    elapsed_seconds: float = 0.0
    timed_out: bool = False
    skipped_reason: str | None = None


@dataclass
class _MutableResult:
    cutoff: datetime
    dry_run: bool
    lock_acquired: bool = False
    eligible_runs: int = 0
    deleted_runs: int = 0
    deleted_events: int = 0
    batches: int = 0
    timed_out: bool = False
    skipped_reason: str | None = None

    def freeze(self, elapsed_seconds: float) -> RetentionResult:
        return RetentionResult(
            cutoff=self.cutoff,
            dry_run=self.dry_run,
            lock_acquired=self.lock_acquired,
            eligible_runs=self.eligible_runs,
            deleted_runs=self.deleted_runs,
            deleted_events=self.deleted_events,
            batches=self.batches,
            elapsed_seconds=elapsed_seconds,
            timed_out=self.timed_out,
            skipped_reason=self.skipped_reason,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditRetention:
    """Run bounded, tenant-agnostic cleanup against the audit tables."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        config: RetentionConfig | None = None,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.pool = pool
        self.config = config or RetentionConfig()
        self._owns_metrics = metrics is None
        self.metrics = metrics or RuntimeMetrics(service_name="langgraph-retention")

    @classmethod
    async def connect(
        cls,
        conninfo: str,
        *,
        config: RetentionConfig | None = None,
        metrics: RuntimeMetrics | None = None,
        min_size: int = 1,
        max_size: int = 1,
    ) -> AuditRetention:
        pool = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            open=False,
            name="agent-audit-retention",
        )
        await pool.open(wait=True)
        return cls(pool, config=config, metrics=metrics)

    async def close(self) -> None:
        await self.pool.close()
        if self._owns_metrics:
            self.metrics.shutdown()

    async def __aenter__(self) -> AuditRetention:
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.close()

    async def purge(
        self,
        *,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> RetentionResult:
        """Delete finished audit runs older than the configured cutoff.

        The advisory lock is held on the same connection for the whole run.
        Each batch is a separate transaction, so a timeout or database error
        leaves already-committed batches intact and the next invocation can
        safely continue.
        """

        started = time.monotonic()
        self.metrics.increment("audit_retention_runs_total")
        reference_now = now or _utc_now()
        if reference_now.tzinfo is None:
            reference_now = reference_now.replace(tzinfo=UTC)
        cutoff = reference_now.astimezone(UTC) - timedelta(days=self.config.retention_days)
        result = _MutableResult(cutoff=cutoff, dry_run=dry_run)

        async with self.pool.connection() as connection:
            result.lock_acquired = await self._try_lock(connection, self.config.lock_key)
            if not result.lock_acquired:
                self.metrics.increment("audit_retention_lock_skips_total")
                result.skipped_reason = "retention_lock_not_acquired"
                self.metrics.observe("audit_retention_duration_seconds", time.monotonic() - started)
                return result.freeze(time.monotonic() - started)
            try:
                try:
                    async with asyncio.timeout(self.config.max_runtime_seconds):
                        await self._purge_locked(connection, result)
                except TimeoutError:
                    result.timed_out = True
                    logger.warning(
                        "audit retention reached max runtime",
                        extra={
                            "retention_days": self.config.retention_days,
                            "deleted_runs": result.deleted_runs,
                            "deleted_events": result.deleted_events,
                        },
                    )
            finally:
                await self._unlock(connection)

        elapsed = time.monotonic() - started
        self.metrics.observe("audit_retention_duration_seconds", elapsed)
        if result.deleted_runs:
            self.metrics.increment("audit_retention_deleted_runs_total", result.deleted_runs)
        if result.deleted_events:
            self.metrics.increment("audit_retention_deleted_events_total", result.deleted_events)
        if result.timed_out:
            self.metrics.increment("audit_retention_timeouts_total")
        return result.freeze(elapsed)

    async def _purge_locked(self, connection, result: _MutableResult) -> None:
        if result.dry_run:
            result.eligible_runs = await self._count_candidates(connection, result.cutoff)
            return

        while True:
            async with connection.transaction():
                run_ids = await self._select_candidate_ids(
                    connection,
                    cutoff=result.cutoff,
                    limit=self.config.batch_size,
                )
                if not run_ids:
                    return

                result.eligible_runs += len(run_ids)
                result.deleted_events += await self._delete_events(connection, run_ids)
                result.deleted_runs += await self._delete_runs(connection, run_ids, result.cutoff)
                result.batches += 1

    @staticmethod
    async def _try_lock(connection, lock_key: int) -> bool:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT pg_try_advisory_lock(%s::bigint)", (lock_key,))
            row = await cursor.fetchone()
        return bool(row and row[0])

    async def _unlock(self, connection) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT pg_advisory_unlock(%s::bigint)", (self.config.lock_key,))

    @staticmethod
    async def _count_candidates(connection, cutoff: datetime) -> int:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT count(*)
                FROM agent_runs
                WHERE status <> 'running'
                  AND finished_at IS NOT NULL
                  AND finished_at < %s
                """,
                (cutoff,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _select_candidate_ids(connection, *, cutoff: datetime, limit: int) -> list[str]:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT run_id
                FROM agent_runs
                WHERE status <> 'running'
                  AND finished_at IS NOT NULL
                  AND finished_at < %s
                ORDER BY finished_at ASC, run_id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (cutoff, limit),
            )
            return [str(row[0]) for row in await cursor.fetchall()]

    @staticmethod
    async def _delete_events(connection, run_ids: Sequence[str]) -> int:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM agent_events WHERE run_id = ANY(%s)", (list(run_ids),)
            )
            return max(cursor.rowcount, 0)

    @staticmethod
    async def _delete_runs(connection, run_ids: Sequence[str], cutoff: datetime) -> int:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                DELETE FROM agent_runs
                WHERE run_id = ANY(%s)
                  AND status <> 'running'
                  AND finished_at IS NOT NULL
                  AND finished_at < %s
                """,
                (list(run_ids), cutoff),
            )
            return max(cursor.rowcount, 0)


@asynccontextmanager
async def retention_context(
    conninfo: str,
    *,
    config: RetentionConfig | None = None,
    metrics: RuntimeMetrics | None = None,
) -> AsyncIterator[AuditRetention]:
    retention = await AuditRetention.connect(conninfo, config=config, metrics=metrics)
    try:
        yield retention
    finally:
        await retention.close()


async def run_retention_once(
    conninfo: str,
    *,
    config: RetentionConfig | None = None,
    metrics: RuntimeMetrics | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RetentionResult:
    async with retention_context(conninfo, config=config, metrics=metrics) as retention:
        return await retention.purge(dry_run=dry_run, now=now)


def _result_json(result: RetentionResult) -> str:
    return json.dumps(
        {
            "cutoff": result.cutoff.isoformat(),
            "dry_run": result.dry_run,
            "lock_acquired": result.lock_acquired,
            "eligible_runs": result.eligible_runs,
            "deleted_runs": result.deleted_runs,
            "deleted_events": result.deleted_events,
            "batches": result.batches,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            "timed_out": result.timed_out,
            "skipped_reason": result.skipped_reason,
        },
        ensure_ascii=False,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清理过期 Agent 审计记录")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    parser.add_argument(
        "--force", action="store_true", help="覆盖 AUDIT_RETENTION_ENABLED=false 的保护"
    )
    parser.add_argument("--retention-days", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--max-runtime-seconds", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    config = RetentionConfig.from_env()
    overrides = {
        key: value
        for key, value in {
            "retention_days": args.retention_days,
            "batch_size": args.batch_size,
            "max_runtime_seconds": args.max_runtime_seconds,
        }.items()
        if value is not None
    }
    if overrides:
        config = replace(config, **overrides)
    if not config.enabled and not args.force:
        result = RetentionResult(
            cutoff=_utc_now() - timedelta(days=config.retention_days),
            dry_run=args.dry_run,
            lock_acquired=False,
            skipped_reason="retention_disabled",
        )
        print(_result_json(result))
        return 0

    metrics = RuntimeMetrics(service_name="langgraph-retention")
    try:
        result = asyncio.run(
            run_retention_once(
                database_url_from_env(),
                config=config,
                dry_run=args.dry_run,
                metrics=metrics,
            )
        )
        print(_result_json(result))
    finally:
        metrics.shutdown()
    if result.timed_out:
        return 2
    return 0 if result.lock_acquired else 3


if __name__ == "__main__":
    raise SystemExit(main())
