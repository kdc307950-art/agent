"""Layered retention for LangGraph store/checkpoint data.

Audit rows are handled by backend.retention. Store rows use LangGraph's native
expires_at. Checkpoints are removed only at whole-thread granularity after the
latest terminal audit run is old enough; partial parent-chain deletion is not
safe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from psycopg_pool import AsyncConnectionPool

from .schema import check_schema_ready


DATA_RETENTION_LOCK_KEY = 891274632


@dataclass(frozen=True)
class DataRetentionConfig:
    checkpoint_days: int = 90
    batch_size: int = 500
    max_runtime_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.checkpoint_days < 1 or self.batch_size < 1 or self.max_runtime_seconds <= 0:
            raise ValueError("数据保留参数无效")


@dataclass(frozen=True)
class DataRetentionResult:
    dry_run: bool
    lock_acquired: bool
    expired_store_rows: int = 0
    deleted_store_rows: int = 0
    eligible_threads: int = 0
    deleted_checkpoints: int = 0
    deleted_checkpoint_writes: int = 0
    deleted_checkpoint_blobs: int = 0


class DataRetention:
    def __init__(self, pool: AsyncConnectionPool, config: DataRetentionConfig) -> None:
        self.pool = pool
        self.config = config

    @classmethod
    async def connect(cls, conninfo: str, config: DataRetentionConfig) -> "DataRetention":
        pool = AsyncConnectionPool(
            conninfo,
            min_size=1,
            max_size=1,
            open=False,
            name="agent-data-retention",
        )
        await pool.open(wait=True)
        return cls(pool, config)

    async def close(self) -> None:
        await self.pool.close()

    async def purge(
        self,
        *,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> DataRetentionResult:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        reference = reference.astimezone(timezone.utc)
        cutoff = reference - timedelta(days=self.config.checkpoint_days)
        result = DataRetentionResult(dry_run=dry_run, lock_acquired=False)
        async with self.pool.connection() as connection:
            await check_schema_ready(connection)
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT pg_try_advisory_lock(%s::bigint)",
                    (DATA_RETENTION_LOCK_KEY,),
                )
                row = await cursor.fetchone()
                if not row or not row[0]:
                    return result
            try:
                async with asyncio.timeout(self.config.max_runtime_seconds):
                    store_count = await self._count_expired_store(connection, reference)
                    thread_count = await self._count_expired_threads(connection, cutoff)
                    result = DataRetentionResult(
                        dry_run=dry_run,
                        lock_acquired=True,
                        expired_store_rows=store_count,
                        eligible_threads=thread_count,
                    )
                    if not dry_run:
                        store_deleted = await self._delete_expired_store(connection, reference)
                        cp, writes, blobs = await self._delete_expired_threads(connection, cutoff)
                        result = DataRetentionResult(
                            dry_run=False,
                            lock_acquired=True,
                            expired_store_rows=store_count,
                            deleted_store_rows=store_deleted,
                            eligible_threads=thread_count,
                            deleted_checkpoints=cp,
                            deleted_checkpoint_writes=writes,
                            deleted_checkpoint_blobs=blobs,
                        )
            finally:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT pg_advisory_unlock(%s::bigint)",
                        (DATA_RETENTION_LOCK_KEY,),
                    )
        return result

    async def _count_expired_store(self, connection, now: datetime) -> int:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT count(*) FROM store WHERE expires_at IS NOT NULL AND expires_at < %s",
                (now,),
            )
            return int((await cursor.fetchone())[0])

    async def _delete_expired_store(self, connection, now: datetime) -> int:
        total = 0
        while True:
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "DELETE FROM store WHERE ctid IN "
                        "(SELECT ctid FROM store WHERE expires_at IS NOT NULL "
                        "AND expires_at < %s LIMIT %s)",
                        (now, self.config.batch_size),
                    )
                    deleted = max(cursor.rowcount, 0)
            total += deleted
            if deleted == 0:
                return total

    async def _count_expired_threads(self, connection, cutoff: datetime) -> int:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT count(*)
                FROM agent_thread_activity AS a
                WHERE a.last_finished_at IS NOT NULL
                  AND a.last_finished_at < %s
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_runs r
                      WHERE r.thread_id = a.thread_id AND r.status = 'running'
                  )
                  AND EXISTS (
                      SELECT 1 FROM checkpoints c WHERE c.thread_id = a.thread_id
                  )
                """,
                (cutoff,),
            )
            return int((await cursor.fetchone())[0])

    async def _delete_expired_threads(
        self,
        connection,
        cutoff: datetime,
    ) -> tuple[int, int, int]:
        total = [0, 0, 0]
        while True:
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT a.thread_id
                        FROM agent_thread_activity AS a
                        WHERE a.last_finished_at IS NOT NULL
                          AND a.last_finished_at < %s
                          AND NOT EXISTS (
                              SELECT 1 FROM agent_runs r
                              WHERE r.thread_id = a.thread_id AND r.status = 'running'
                          )
                          AND EXISTS (
                              SELECT 1 FROM checkpoints c WHERE c.thread_id = a.thread_id
                          )
                        ORDER BY a.thread_id
                        LIMIT %s
                        """,
                        (cutoff, self.config.batch_size),
                    )
                    thread_ids = [str(row[0]) for row in await cursor.fetchall()]
                    if not thread_ids:
                        return tuple(total)
                    await cursor.execute(
                        "DELETE FROM checkpoint_writes WHERE thread_id = ANY(%s)",
                        (thread_ids,),
                    )
                    total[1] += max(cursor.rowcount, 0)
                    await cursor.execute(
                        "DELETE FROM checkpoint_blobs WHERE thread_id = ANY(%s)",
                        (thread_ids,),
                    )
                    total[2] += max(cursor.rowcount, 0)
                    await cursor.execute(
                        "DELETE FROM checkpoints WHERE thread_id = ANY(%s)",
                        (thread_ids,),
                    )
                    total[0] += max(cursor.rowcount, 0)
                    await cursor.execute(
                        "DELETE FROM agent_thread_activity a "
                        "WHERE a.thread_id = ANY(%s) "
                        "AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.thread_id = a.thread_id)",
                        (thread_ids,),
                    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分层清理 LangGraph store/checkpoint")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument(
        "--checkpoint-days",
        type=int,
        default=int(os.getenv("CHECKPOINT_RETENTION_DAYS", "90")),
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-runtime-seconds", type=float, default=60)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.database_url:
        raise SystemExit("--database-url 或 DATABASE_URL 必须配置")
    config = DataRetentionConfig(
        args.checkpoint_days,
        args.batch_size,
        args.max_runtime_seconds,
    )

    async def run() -> DataRetentionResult:
        retention = await DataRetention.connect(args.database_url, config)
        try:
            return await retention.purge(dry_run=args.dry_run)
        finally:
            await retention.close()

    result = asyncio.run(run())
    print(json.dumps(result.__dict__, ensure_ascii=False))
    return 0 if result.lock_acquired else 3


if __name__ == "__main__":
    raise SystemExit(main())
