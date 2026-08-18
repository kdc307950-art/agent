from __future__ import annotations

import asyncio
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from .settings import database_url_from_env


async def migrate_checkpoint_tuples(source, target) -> int:
    items = list(source.list(None))
    items.reverse()
    for item in items:
        base_config = item.parent_config or {
            "configurable": {
                "thread_id": item.config["configurable"]["thread_id"],
                "checkpoint_ns": item.config["configurable"].get("checkpoint_ns", ""),
            }
        }
        target_config = await target.aput(
            base_config,
            item.checkpoint,
            item.metadata,
            item.checkpoint.get("channel_versions", {}),
        )
        grouped_writes = defaultdict(list)
        for task_id, channel, value in item.pending_writes or []:
            grouped_writes[task_id].append((channel, value))
        for task_id, writes in grouped_writes.items():
            await target.aput_writes(target_config, writes, task_id)
    return len(items)


async def migrate_sqlite_to_postgres() -> int:
    sqlite_path = Path(os.getenv("SQLITE_CHECKPOINT_PATH", "checkpoints.db"))
    if not sqlite_path.is_file():
        raise RuntimeError(f"SQLite checkpoint 文件不存在: {sqlite_path}")

    database_url = database_url_from_env()
    connection = sqlite3.connect(
        f"file:{sqlite_path.resolve().as_posix()}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    source = SqliteSaver(connection)
    try:
        async with AsyncPostgresSaver.from_conn_string(database_url) as target:
            await target.setup()
            return await migrate_checkpoint_tuples(source, target)
    finally:
        connection.close()


if __name__ == "__main__":
    migrated = asyncio.run(migrate_sqlite_to_postgres())
    print(f"Migrated checkpoints: {migrated}")
