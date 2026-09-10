"""VPN reissue Outbox（backend/vpn/outbox.py）的 PostgreSQL 集成测试。

复用仓库**既有**的发件箱 ``outbox_events`` 表与通用消费 Worker（``OutboxWorker`` +
``TicketOperationsRepository``）：本文件只验证「发布 → 通用 worker 领取并投递」链路，
以及「worker 失败 → 指数退避重试 → 超过次数进死信」。

仅当提供 TEST_DATABASE_URL（或 DATABASE_URL，对齐 test_vpn_reissue_store_postgres.py
约定）时运行；否则整文件跳过（collect 0 失败）。

覆盖：
    - publish 入箱（aggregate_type='vpn_reissue'）后由通用 worker 投递为 delivered；
    - 幂等键 (tenant_id, idempotency_key=operation_id:event_type) 去重，重复发布不重复入箱；
    - sender 抛瞬时失败 → worker 用 available_at 退避重试；超过 max_attempts → 死信（dead）。
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from backend.migrations import setup_postgres
from backend.outbox_worker import OutboxWorker, TransientDeliveryError
from backend.tickets import TicketOperationsRepository
from backend.vpn.outbox import AGGREGATE_TYPE, PostgresReissueOutbox, ReissueEventType

DATABASE_URL = os.getenv("TEST_DATABASE_URL", os.getenv("DATABASE_URL", "")).strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="需要 TEST_DATABASE_URL（compose.test 55436）才能运行 reissue Outbox 集成测试",
)


async def _purge_outbox_events() -> None:
    """清空 outbox_events 表，避免跨测试残留影响 worker claim 计数与退避断言。"""
    from psycopg import AsyncConnection

    from backend.migrations import setup_postgres

    await setup_postgres()  # 确保 schema 存在（tmpfs 容器可能被重建/清空）
    async with await AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM outbox_events")


@pytest.fixture(autouse=True)
def _purge_outbox():
    asyncio.run(_purge_outbox_events())


async def _open_pool() -> AsyncConnectionPool:
    os.environ.setdefault("DATABASE_URL", DATABASE_URL)
    await setup_postgres()
    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=False, name="vpn-outbox")
    await pool.open(wait=True)
    return pool


def _tenant() -> str:
    return f"tenant-{uuid4().hex}"


def _op(prefix: str = "op") -> str:
    return f"{prefix}-{uuid4().hex}"


class _RecordingSender:
    """记录投递事件的 sender（用于断言 delivered 且不实际发 HTTP）。"""

    def __init__(self, error: Exception | None = None):
        self.events: list[dict] = []
        self.error = error

    async def send(self, event):
        self.events.append(dict(event))
        if self.error is not None:
            raise self.error


async def _cleanup(pool: AsyncConnectionPool, tenant_id: str) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM outbox_events WHERE tenant_id = %s", (tenant_id,))


def test_publish_then_worker_delivers():
    """发布一条 vpn_reissue 事件 → 通用 worker 领取并成功投递为 delivered。"""

    async def run():
        pool = await _open_pool()
        try:
            repo = TicketOperationsRepository(pool)
            outbox = PostgresReissueOutbox(pool)
            tenant = _tenant()
            operation_id = _op()
            # 发布：operation_confirmed。
            inserted = await outbox.publish(
                tenant_id=tenant,
                operation_id=operation_id,
                event_type=ReissueEventType.OPERATION_CONFIRMED,
                payload={"status": "confirmed", "ok": True, "delivered": True, "confirmed": True},
            )
            assert inserted is True
            # 复用既有 worker，路由到记录型 sender。
            sender = _RecordingSender()
            worker = OutboxWorker(repo, {ReissueEventType.OPERATION_CONFIRMED.value: sender})
            result = await worker.run_once(limit=10, tenant_id=tenant)
            assert result.claimed == 1
            assert result.delivered == 1
            assert result.dead == 0
            # 事件已被投递（sender 收到原始行）。
            assert len(sender.events) == 1
            assert sender.events[0]["event_type"] == "operation_confirmed"
            assert sender.events[0]["aggregate_type"] == AGGREGATE_TYPE
            assert sender.events[0]["aggregate_id"] == operation_id
            assert sender.events[0]["idempotency_key"] == f"{operation_id}:operation_confirmed"
            # 终态：delivered。
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT status FROM outbox_events WHERE tenant_id = %s AND aggregate_id = %s",
                        (tenant, operation_id),
                    )
                    row = await cur.fetchone()
                    assert row["status"] == "delivered"
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_publish_is_idempotent_by_idempotency_key():
    """同一 (operation_id, event_type) 重复发布不重复入箱（ON CONFLICT DO NOTHING）。"""

    async def run():
        pool = await _open_pool()
        try:
            outbox = PostgresReissueOutbox(pool)
            tenant = _tenant()
            operation_id = _op()
            payload = {"status": "confirmed"}
            first = await outbox.publish(
                tenant_id=tenant,
                operation_id=operation_id,
                event_type=ReissueEventType.OPERATION_CONFIRMED,
                payload=payload,
            )
            second = await outbox.publish(
                tenant_id=tenant,
                operation_id=operation_id,
                event_type=ReissueEventType.OPERATION_CONFIRMED,
                payload=payload,
            )
            assert first is True
            assert second is False
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT count(*) AS n FROM outbox_events WHERE tenant_id = %s",
                        (tenant,),
                    )
                    row = await cur.fetchone()
                    assert row["n"] == 1
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_worker_transient_failure_retries_then_dead():
    """sender 抛瞬时失败 → worker 退避重试；超过 max_attempts → 死信（dead）。"""

    async def run():
        pool = await _open_pool()
        try:
            repo = TicketOperationsRepository(pool)
            outbox = PostgresReissueOutbox(pool)
            tenant = _tenant()
            operation_id = _op()
            await outbox.publish(
                tenant_id=tenant,
                operation_id=operation_id,
                event_type=ReissueEventType.OPERATION_FAILED,
                payload={"status": "failed", "error_code": "vendor_error"},
            )
            # 始终抛瞬时失败（可重试）的 sender。
            sender = _RecordingSender(error=TransientDeliveryError("temporary_network"))
            worker = OutboxWorker(
                repo,
                {ReissueEventType.OPERATION_FAILED.value: sender},
                max_attempts=3,
            )
            # 第 1 轮：首次失败 → 退避重试（pending，available_at 后移）。
            r1 = await worker.run_once(limit=10, tenant_id=tenant)
            assert r1.claimed == 1 and r1.retried == 1
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    # 读取当前状态与可用时间。
                    await cur.execute(
                        "SELECT status, attempts, available_at, last_error_code FROM outbox_events WHERE tenant_id = %s",
                        (tenant,),
                    )
                    row = await cur.fetchone()
            # 首轮仍 pending（进入退避），attempts 递增，error_code 记录成瞬时错误。
            assert row["status"] == "pending"
            assert row["attempts"] >= 1
            # worker 对未携带 error_code 的瞬时异常回退到异常类型名（见 OutboxWorker._fenced_fail）。
            assert row["last_error_code"] == "TransientDeliveryError"
            # 第 2 轮：把 available_at 拨到过去，再次领取（模拟退避到期）。
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE outbox_events SET available_at = now() - interval '1 second' WHERE tenant_id = %s",
                        (tenant,),
                    )
            r2 = await worker.run_once(limit=10, tenant_id=tenant)
            assert r2.claimed == 1
            # max_attempts=3：经过多轮仍瞬时失败，最后一次达到上限 → dead。
            # 逐轮推进 available_at 直到进入 dead。
            last_status = None
            for _ in range(4):
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE outbox_events SET available_at = now() - interval '1 second' WHERE tenant_id = %s",
                            (tenant,),
                        )
                await worker.run_once(limit=10, tenant_id=tenant)
                async with pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            "SELECT status FROM outbox_events WHERE tenant_id = %s",
                            (tenant,),
                        )
                        row = await cur.fetchone()
                        last_status = row["status"]
                        if last_status == "dead":
                            break
            assert last_status == "dead"
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())
