"""数据迁移工具 —— 把 SQLite checkpointer 数据迁移到 Postgres。

用途：
    开发期用 SQLite 跑通后，需要切到 Postgres 持久化时，
    用 migrate_sqlite_to_postgres() 将已有 checkpoint 全部搬过去。

注意：
    - 需要 DATABASE_URL（Postgres）与默认 SQLite 路径（checkpoints.db）
    - 迁移是追加式（insert），不会覆盖目标数据
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from .settings import database_url_from_env
from .repositories import tenant_thread_id


async def migrate_checkpoint_tuples(source, target, thread_id_mapper=None) -> int:
    items = list(source.list(None))
    items.reverse()
    for item in items:
        source_config = item.parent_config or {
            "configurable": {
                "thread_id": item.config["configurable"]["thread_id"],
                "checkpoint_ns": item.config["configurable"].get("checkpoint_ns", ""),
            }
        }
        base_config = {
            **source_config,
            "configurable": dict(source_config["configurable"]),
        }
        if thread_id_mapper is not None:
            base_config["configurable"]["thread_id"] = thread_id_mapper(
                base_config["configurable"]["thread_id"]
            )
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
    tenant_id = os.getenv("MIGRATION_TENANT_ID", "").strip()
    user_id = os.getenv("MIGRATION_USER_ID", "").strip()
    if not tenant_id or not user_id:
        raise RuntimeError("迁移旧 checkpoint 必须设置 MIGRATION_TENANT_ID 和 MIGRATION_USER_ID")
    connection = sqlite3.connect(
        f"file:{sqlite_path.resolve().as_posix()}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    source = SqliteSaver(connection)
    try:
        async with AsyncPostgresSaver.from_conn_string(database_url) as target:
            await target.setup()
            return await migrate_checkpoint_tuples(
                source,
                target,
                lambda old_thread_id: tenant_thread_id(tenant_id, user_id, old_thread_id),
            )
    finally:
        connection.close()


if __name__ == "__main__":
    migrated = asyncio.run(migrate_sqlite_to_postgres())
    print(f"Migrated checkpoints: {migrated}")
