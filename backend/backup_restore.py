"""PostgreSQL 备份/恢复冒烟测试工具。

说明：应用本身不在请求链路里做备份，运维人员从定时任务或恢复工作站
（装有 PostgreSQL 客户端工具，或数据库容器内）运行本模块：
    create_backup / restore_backup / verify_recovery
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path
from typing import Sequence

from psycopg import AsyncConnection

from .repositories import tenant_namespace, tenant_thread_id
from .schema import check_schema_ready


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"找不到 PostgreSQL 工具 {command[0]}，请安装客户端或在 PostgreSQL 容器内执行"
        ) from exc


def create_backup(database_url: str, output: Path, *, pg_dump_bin: str = "pg_dump") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    _run([pg_dump_bin, "--format=custom", "--no-owner", "--file", str(output), database_url])


def restore_backup(
    backup: Path,
    database_url: str,
    *,
    pg_restore_bin: str = "pg_restore",
    clean: bool = True,
) -> None:
    command = [pg_restore_bin, "--no-owner", "--exit-on-error"]
    if clean:
        command.extend(["--clean", "--if-exists"])
    command.extend(["--dbname", database_url, str(backup)])
    _run(command)


async def verify_recovery(
    database_url: str,
    *,
    tenant_id: str,
    user_id: str,
    client_thread_id: str,
    memory_key: str,
) -> dict[str, object]:
    """Verify checkpoint and store rows survived a restore.

    The probe intentionally does not invoke a live model.  It proves that the
    durable state needed for a subsequent Agent run is present and tenant
    scoped; a separate live E2E test verifies provider execution.
    """

    thread_id = tenant_thread_id(tenant_id, user_id, client_thread_id)
    prefix = ".".join(tenant_namespace(tenant_id, user_id))
    async with await AsyncConnection.connect(database_url) as connection:
        await check_schema_ready(connection)
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id = %s",
                (thread_id,),
            )
            checkpoint_count = int((await cursor.fetchone())[0])
            await cursor.execute(
                "SELECT count(*) FROM store WHERE prefix = %s AND key = %s",
                (prefix, memory_key),
            )
            memory_count = int((await cursor.fetchone())[0])
    return {
        "thread_id": thread_id,
        "checkpoint_count": checkpoint_count,
        "memory_count": memory_count,
        "ok": checkpoint_count > 0 and memory_count > 0,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PostgreSQL backup/restore drill")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--pg-dump-bin", default=os.getenv("PG_DUMP_BIN", "pg_dump"))

    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--database-url", required=True)
    restore.add_argument("--pg-restore-bin", default=os.getenv("PG_RESTORE_BIN", "pg_restore"))

    verify = subparsers.add_parser("verify")
    verify.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    verify.add_argument("--tenant-id", required=True)
    verify.add_argument("--user-id", required=True)
    verify.add_argument("--thread-id", required=True)
    verify.add_argument("--memory-key", default="preferences")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "backup":
        if not args.database_url:
            raise SystemExit("--database-url 或 DATABASE_URL 必须配置")
        create_backup(args.database_url, args.output, pg_dump_bin=args.pg_dump_bin)
        print(args.output)
        return 0
    if args.command == "restore":
        restore_backup(args.backup, args.database_url, pg_restore_bin=args.pg_restore_bin)
        return 0
    if not args.database_url:
        raise SystemExit("--database-url 或 DATABASE_URL 必须配置")
    result = asyncio.run(
        verify_recovery(
            args.database_url,
            tenant_id=args.tenant_id,
            user_id=args.user_id,
            client_thread_id=args.thread_id,
            memory_key=args.memory_key,
        )
    )
    print(result)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
