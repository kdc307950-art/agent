from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from backend.audit import audit_context
from backend.retention import AuditRetention, RetentionConfig
from backend.run_context import RunContext


DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def _context(run_id: str, tenant_id: str = "retention-test") -> RunContext:
    return RunContext(
        run_id=run_id,
        request_id=f"request-{run_id}",
        tenant_id=tenant_id,
        user_id="user-1",
        thread_id=f"{tenant_id}:user-1:thread-1",
        scopes=frozenset({"chat:read", "chat:write"}),
        deadline=asyncio.get_running_loop().time() + 60,
    )


async def _seed_runs(run_ids: list[str], *, now: datetime) -> str:
    old_finished = now - timedelta(days=90)
    async with audit_context(DATABASE_URL) as audit:
        await audit.setup()
        for run_id in run_ids:
            await audit.start_run(_context(run_id))
            await audit.finish_run(_context(run_id), "completed")
            async with audit.pool.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE agent_runs
                        SET started_at = %s, finished_at = %s
                        WHERE run_id = %s
                        """,
                        (old_finished - timedelta(minutes=1), old_finished, run_id),
                    )
    return old_finished.isoformat()


async def _seed_running(run_id: str, *, now: datetime) -> None:
    old_started = now - timedelta(days=90)
    async with audit_context(DATABASE_URL) as audit:
        await audit.setup()
        await audit.start_run(_context(run_id))
        async with audit.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "UPDATE agent_runs SET started_at = %s WHERE run_id = %s",
                    (old_started, run_id),
                )


async def _get_run(run_id: str) -> dict | None:
    async with audit_context(DATABASE_URL) as audit:
        return await audit.get_run("retention-test", run_id)


async def _delete_test_runs(run_ids: list[str]) -> None:
    async with audit_context(DATABASE_URL) as audit:
        async with audit.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("DELETE FROM agent_events WHERE run_id = ANY(%s)", (run_ids,))
                await cursor.execute("DELETE FROM agent_runs WHERE run_id = ANY(%s)", (run_ids,))


def test_retention_dry_run_then_bounded_idempotent_cleanup_preserves_running() -> None:
    now = datetime.now(timezone.utc)
    completed_ids = [f"retention-completed-{uuid4().hex}" for _ in range(3)]
    running_id = f"retention-running-{uuid4().hex}"
    all_ids = [*completed_ids, running_id]

    async def run():
        await _seed_runs(completed_ids, now=now)
        await _seed_running(running_id, now=now)
        config = RetentionConfig(
            retention_days=30,
            batch_size=2,
            max_runtime_seconds=20,
            enabled=True,
        )
        retention = await AuditRetention.connect(DATABASE_URL, config=config)
        async with retention:
            dry_run = await retention.purge(dry_run=True, now=now)
            first = await retention.purge(now=now)
            second = await retention.purge(now=now)
        completed_after = [await _get_run(run_id) for run_id in completed_ids]
        running_after = await _get_run(running_id)
        await _delete_test_runs(all_ids)
        return dry_run, first, second, completed_after, running_after

    dry_run, first, second, completed_after, running_after = asyncio.run(run())
    assert dry_run.lock_acquired is True
    assert dry_run.dry_run is True
    assert dry_run.eligible_runs == 3
    assert dry_run.deleted_runs == 0
    assert first.deleted_runs == 3
    assert first.deleted_events >= 3
    assert first.batches == 2
    assert all(item is None for item in completed_after)
    assert running_after is not None
    assert running_after["status"] == "running"
    assert second.lock_acquired is True
    assert second.deleted_runs == 0
    assert second.batches == 0


def test_retention_skips_when_advisory_lock_is_held() -> None:
    now = datetime.now(timezone.utc)
    config = RetentionConfig(retention_days=30, batch_size=10, max_runtime_seconds=20, enabled=True)

    async def run():
        lock_pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=1, open=False, name="retention-lock-test")
        await lock_pool.open(wait=True)
        try:
            async with lock_pool.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT pg_advisory_lock(%s::bigint)", (config.lock_key,))
                retention = await AuditRetention.connect(DATABASE_URL, config=config)
                async with retention:
                    result = await retention.purge(now=now)
            return result
        finally:
            await lock_pool.close()

    result = asyncio.run(run())
    assert result.lock_acquired is False
    assert result.skipped_reason == "retention_lock_not_acquired"


def test_retention_config_rejects_invalid_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_RETENTION_BATCH_SIZE", "0")
    with pytest.raises(RuntimeError, match="AUDIT_RETENTION_BATCH_SIZE"):
        RetentionConfig.from_env()
