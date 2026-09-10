"""VPN 重新下发审批式执行 —— ReissueStore（PostgreSQL 后端）集成断言。

背景（生产基础修复 Phase 1 / eng-store t1）：
    ``PostgresReissueStore`` 是审批状态持久化后端：vpn_reissue_operations 表 + 租户隔离 +
    ``set_status`` 条件 UPDATE（CAS）+ ``claim_reconcilable`` 的 FOR UPDATE SKIP LOCKED。
    本文件在真实 PostgreSQL 测试栈上补齐这些断言，证明「跨进程存活 + 原子转移 + 进程正确争抢」。

范围（仅 ReissueStore；不触发真实 LLM / 工单 / 审计）：
    (a) create_operation 确实落一行到 vpn_reissue_operations；
    (b) set_status CAS：from_status 与当前不符 → ILLEGAL_TRANSITION 且该行不变；
    (c) 两个并发 set_status PENDING→APPROVED → 恰好一个 SUCCESS（验证 WHERE status='pending' 条件更新串行化）；
    (d) 跨租户 get_operation → None（租户隔离）；
    (e) 两个 worker（两连接）claim_reconcilable → 领取集合不相交（FOR UPDATE SKIP LOCKED）。

约定：``pytestmark = pytest.mark.skipif(not (TEST_DATABASE_URL or DATABASE_URL), reason="需要 TEST_DATABASE_URL")``，
无 DB 时整文件自动跳过（对齐 tests/test_vpn_protection_postgres.py）。
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from backend.vpn.approval import ApprovalStatus
from backend.vpn.reissue_store import PostgresReissueStore, ReissueTransitionResult

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not (TEST_DATABASE_URL or DATABASE_URL),
    reason="需要 TEST_DATABASE_URL",
)


async def _open_pool(name: str = "vpn-reissue-store") -> AsyncConnectionPool:
    """建库（幂等 setup_postgres）+ 打开连接池。"""
    os.environ.setdefault("DATABASE_URL", DATABASE_URL)
    from backend.migrations import setup_postgres

    await setup_postgres()
    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=False, name=name)
    await pool.open(wait=True)
    return pool


def _store(pool: AsyncConnectionPool) -> PostgresReissueStore:
    return PostgresReissueStore(pool)


async def _create_pending(
    store: PostgresReissueStore, *, tenant_id: str, operation_id: str
) -> None:
    await store.create_operation(
        tenant_id=tenant_id,
        ticket_id="t-1",
        operation_id=operation_id,
        idempotency_key=operation_id,
        request_snapshot={"action": "reissue_vpn_config", "ticket_id": "t-1"},
        status=ApprovalStatus.PENDING,
        expected_version=0,
    )


# ===========================================================================
# (a) create_operation 持久化一行
# ===========================================================================


def test_create_operation_persists_row():
    async def run():
        pool = await _open_pool()
        try:
            store = _store(pool)
            tenant = f"tenant-{uuid4().hex}"
            op_id = f"op-{uuid4().hex[:10]}"
            created = await store.create_operation(
                tenant_id=tenant,
                ticket_id="t-1",
                operation_id=op_id,
                idempotency_key=op_id,
                request_snapshot={"action": "reissue_vpn_config", "user_id": "user-042"},
                status=ApprovalStatus.PENDING,
                expected_version=0,
            )
            assert created.tenant_id == tenant
            assert created.operation_id == op_id
            assert created.idempotency_key == op_id
            assert created.status == ApprovalStatus.PENDING
            assert created.request_snapshot["user_id"] == "user-042"

            # 从 DB 读回：仍存在，且列为实际值
            fetched = await store.get_operation(tenant_id=tenant, operation_id=op_id)
            assert fetched is not None
            assert fetched.idempotency_key == op_id
            assert fetched.status == ApprovalStatus.PENDING
        finally:
            await pool.close()

    asyncio.run(run())


# ===========================================================================
# (b) set_status CAS：from_status 不符 → ILLEGAL_TRANSITION 且行不变
# ===========================================================================


def test_set_status_cas_wrong_from_status_illegal_and_unchanged():
    async def run():
        pool = await _open_pool()
        try:
            store = _store(pool)
            tenant = f"tenant-{uuid4().hex}"
            op_id = f"op-{uuid4().hex[:10]}"
            await _create_pending(store, tenant_id=tenant, operation_id=op_id)

            # 当前 pending；从 approved 推进 → 0 行命中 → ILLEGAL_TRANSITION（非 SUCCESS）
            result = await store.set_status(
                tenant_id=tenant,
                operation_id=op_id,
                from_status=ApprovalStatus.APPROVED,
                to_status=ApprovalStatus.EXECUTING,
            )
            assert result in (
                ReissueTransitionResult.ILLEGAL_TRANSITION,
                ReissueTransitionResult.VERSION_CONFLICT,
            ), result
            # 行不变（仍 pending）
            op = await store.get_operation(tenant_id=tenant, operation_id=op_id)
            assert op is not None
            assert op.status == ApprovalStatus.PENDING
        finally:
            await pool.close()

    asyncio.run(run())


# ===========================================================================
# (c) 两并发 set_status PENDING→APPROVED：恰好一个 SUCCESS（WHERE status='pending' 串行化）
# ===========================================================================


def test_concurrent_set_status_only_one_success():
    async def run():
        pool = await _open_pool()
        try:
            store = _store(pool)
            tenant = f"tenant-{uuid4().hex}"
            op_id = f"op-{uuid4().hex[:10]}"
            await _create_pending(store, tenant_id=tenant, operation_id=op_id)

            results = await asyncio.gather(
                store.set_status(
                    tenant_id=tenant,
                    operation_id=op_id,
                    from_status=ApprovalStatus.PENDING,
                    to_status=ApprovalStatus.APPROVED,
                ),
                store.set_status(
                    tenant_id=tenant,
                    operation_id=op_id,
                    from_status=ApprovalStatus.PENDING,
                    to_status=ApprovalStatus.APPROVED,
                ),
            )
            successes = [r for r in results if r == ReissueTransitionResult.SUCCESS]
            assert len(successes) == 1, f"应恰好一个 SUCCESS，实际: {results}"
            # 终态为 approved（二次更新要么 SUCCESS 要么 ALREADY_TARGET_STATE）
            assert all(
                r in (ReissueTransitionResult.SUCCESS, ReissueTransitionResult.ALREADY_TARGET_STATE)
                for r in results
            )
            op = await store.get_operation(tenant_id=tenant, operation_id=op_id)
            assert op is not None and op.status == ApprovalStatus.APPROVED
        finally:
            await pool.close()

    asyncio.run(run())


# ===========================================================================
# (d) 跨租户 get_operation → None
# ===========================================================================


def test_get_operation_cross_tenant_returns_none():
    async def run():
        pool = await _open_pool()
        try:
            store = _store(pool)
            tenant_a = f"tenant-a-{uuid4().hex}"
            tenant_b = f"tenant-b-{uuid4().hex}"
            op_id = f"op-{uuid4().hex[:10]}"
            await _create_pending(store, tenant_id=tenant_a, operation_id=op_id)

            # 同租户命中
            assert await store.get_operation(tenant_id=tenant_a, operation_id=op_id) is not None
            # 跨租户 → None（不得读到他人操作）
            assert await store.get_operation(tenant_id=tenant_b, operation_id=op_id) is None
        finally:
            await pool.close()

    asyncio.run(run())


# ===========================================================================
# (e) claim_reconcilable：两 worker（两连接）领取集合不相交（FOR UPDATE SKIP LOCKED）
# ===========================================================================


def test_claim_reconcilable_two_workers_disjoint():
    async def run():
        pool = await _open_pool()
        try:
            store = _store(pool)
            tenant = f"tenant-{uuid4().hex}"
            our_op_ids = set()
            for _i in range(2):
                op_id = f"op-recon-{uuid4().hex[:8]}"
                our_op_ids.add(op_id)
                await store.create_operation(
                    tenant_id=tenant,
                    ticket_id="t-1",
                    operation_id=op_id,
                    idempotency_key=op_id,
                    request_snapshot={"action": "reissue_vpn_config"},
                    status=ApprovalStatus.EXECUTION_UNKNOWN,
                    expected_version=0,
                )

            async def claim(worker: str, limit: int):
                return await store.claim_reconcilable(
                    worker_id=worker, lease_seconds=60, limit=limit
                )

            w1, w2 = await asyncio.gather(claim("worker-1", 100), claim("worker-2", 100))
            ids1 = {o.operation_id for o in w1}
            ids2 = {o.operation_id for o in w2}

            # 关键：同一行不会被两个 worker 领取（SKIP LOCKED）
            assert ids1.isdisjoint(ids2), f"worker 领取集合重叠: {ids1} ∩ {ids2}"
            # 本测试创建的可对账行应至少被一位 worker 领取（确认断言非空、SET 确实命中）
            assert (ids1 | ids2) & our_op_ids, "本租户创建的可对账行未被任何 worker 领取"
        finally:
            await pool.close()

    asyncio.run(run())
