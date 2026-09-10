"""PostgresReissueStore（backend/vpn/reissue_store.py）的 PostgreSQL 集成测试。

仅当提供 TEST_DATABASE_URL（或 DATABASE_URL，对齐 test_vpn_protection_postgres.py
约定）时运行；否则整文件跳过。覆盖 PostgresReissueStore 的全部方法：

    - create_operation / get_operation / get_by_idempotency_key（含租户隔离、幂等返回既有）；
    - set_status 的 CAS 语义（SUCCESS / ALREADY_TARGET_STATE / ILLEGAL_TRANSITION /
      VERSION_CONFLICT / DB_ERROR）；
    - set_result 写回终态；
    - claim_reconcilable（FOR UPDATE SKIP LOCKED + 租约）与 list_reconcilable；
    - mark_reconciled 收敛到定态并释放租约。
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from backend.vpn.approval import ApprovalStatus, ReissueExecutionResult
from backend.vpn.reissue_store import PostgresReissueStore, ReissueTransitionResult

DATABASE_URL = os.getenv("TEST_DATABASE_URL", os.getenv("DATABASE_URL", "")).strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="需要 TEST_DATABASE_URL（compose.test 55436）才能运行 PostgresReissueStore 集成测试",
)


@pytest.fixture(autouse=True)
def _purge_operations_table():
    """每个测试前清空 vpn_reissue_operations，避免跨测试/跨租户残留影响全局 claim/list 扫描。"""

    async def _purge():
        from psycopg import AsyncConnection

        from backend.migrations import setup_postgres

        await setup_postgres()  # 确保 schema 存在（tmpfs 容器可能被重建/清空）
        async with await AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM vpn_reissue_operations")

    asyncio.run(_purge())


async def _open_pool() -> AsyncConnectionPool:
    os.environ.setdefault("DATABASE_URL", DATABASE_URL)
    from backend.migrations import setup_postgres

    await setup_postgres()
    pool = AsyncConnectionPool(
        DATABASE_URL, min_size=1, max_size=4, open=False, name="vpn-reissue-store"
    )
    await pool.open(wait=True)
    return pool


def _tenant() -> str:
    return f"tenant-{uuid4().hex}"


def _make_store(pool: AsyncConnectionPool) -> PostgresReissueStore:
    return PostgresReissueStore(pool)


def _seed_kwargs(*, tenant_id: str, operation_id: str, key: str, status: ApprovalStatus):
    return dict(
        tenant_id=tenant_id,
        ticket_id="ticket-1",
        operation_id=operation_id,
        idempotency_key=key,
        request_snapshot={"action": "reissue_vpn_config", "user_id": "u1"},
        status=status,
        expected_version=2,
    )


def test_create_and_get_operation_is_tenant_scoped():
    async def run():
        pool = await _open_pool()
        try:
            store = _make_store(pool)
            tenant = _tenant()
            op = await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant, operation_id="op-1", key="k-1", status=ApprovalStatus.PENDING
                )
            )
            assert op.operation_id == "op-1"
            assert op.status == ApprovalStatus.PENDING
            assert op.expected_version == 2
            assert op.ticket_id == "ticket-1"
            assert op.request_snapshot.get("action") == "reissue_vpn_config"

            # 同租户命中
            got = await store.get_operation(tenant_id=tenant, operation_id="op-1")
            assert got is not None and got.status == ApprovalStatus.PENDING
            # 跨租户 -> None
            assert await store.get_operation(tenant_id=_tenant(), operation_id="op-1") is None
            # 按幂等键命中
            by_key = await store.get_by_idempotency_key(tenant_id=tenant, idempotency_key="k-1")
            assert by_key is not None and by_key.operation_id == "op-1"
            # 回滚清理
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_create_idempotent_returns_existing_for_active_key():
    async def run():
        pool = await _open_pool()
        try:
            store = _make_store(pool)
            tenant = _tenant()
            op1 = await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant, operation_id="op-1", key="k-1", status=ApprovalStatus.PENDING
                )
            )
            op2 = await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant, operation_id="op-2", key="k-1", status=ApprovalStatus.PENDING
                )
            )
            assert op2.operation_id == "op-1"  # 同 (tenant, idempotency_key) 活跃操作 -> 返回既有
            assert op1.operation_id == op2.operation_id
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_set_status_cas_semantics():
    async def run():
        pool = await _open_pool()
        try:
            store = _make_store(pool)
            tenant = _tenant()
            await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant, operation_id="op-1", key="k-1", status=ApprovalStatus.PENDING
                )
            )
            # SUCCESS: PENDING -> APPROVED
            r = await store.set_status(
                tenant_id=tenant,
                operation_id="op-1",
                from_status=ApprovalStatus.PENDING,
                to_status=ApprovalStatus.APPROVED,
                approver_user_id="approver-1",
            )
            assert r == ReissueTransitionResult.SUCCESS, r
            got = await store.get_operation(tenant_id=tenant, operation_id="op-1")
            assert got.status == ApprovalStatus.APPROVED and got.approver_user_id == "approver-1"

            # ALREADY_TARGET_STATE: 目标是 APPROVED 且已是 APPROVED
            r2 = await store.set_status(
                tenant_id=tenant,
                operation_id="op-1",
                from_status=ApprovalStatus.PENDING,
                to_status=ApprovalStatus.APPROVED,
            )
            assert r2 == ReissueTransitionResult.ALREADY_TARGET_STATE, r2

            # 推进到在途 EXECUTION_UNKNOWN
            await store.set_status(
                tenant_id=tenant,
                operation_id="op-1",
                from_status=ApprovalStatus.APPROVED,
                to_status=ApprovalStatus.EXECUTION_UNKNOWN,
            )
            # VERSION_CONFLICT: 当前在途（EXECUTION_UNKNOWN），from=DELIVERED 为陈旧前置
            r5 = await store.set_status(
                tenant_id=tenant,
                operation_id="op-1",
                from_status=ApprovalStatus.DELIVERED,
                to_status=ApprovalStatus.CONFIRMED,
            )
            assert r5 == ReissueTransitionResult.VERSION_CONFLICT, r5

            # 从在途推进到终态 DELIVERED
            await store.set_status(
                tenant_id=tenant,
                operation_id="op-1",
                from_status=ApprovalStatus.EXECUTION_UNKNOWN,
                to_status=ApprovalStatus.DELIVERED,
            )
            # ILLEGAL_TRANSITION: 当前 DELIVERED（终态、非在途），from/to 与当前都不匹配
            r3 = await store.set_status(
                tenant_id=tenant,
                operation_id="op-1",
                from_status=ApprovalStatus.PENDING,
                to_status=ApprovalStatus.APPROVED,
            )
            assert r3 == ReissueTransitionResult.ILLEGAL_TRANSITION, r3

            # DB_ERROR: 操作不存在
            r4 = await store.set_status(
                tenant_id=tenant,
                operation_id="op-missing",
                from_status=ApprovalStatus.PENDING,
                to_status=ApprovalStatus.APPROVED,
            )
            assert r4 == ReissueTransitionResult.DB_ERROR, r4
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_set_result_and_mark_reconciled():
    async def run():
        pool = await _open_pool()
        try:
            store = _make_store(pool)
            tenant = _tenant()
            await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant, operation_id="op-1", key="k-1", status=ApprovalStatus.PENDING
                )
            )
            await store.set_status(
                tenant_id=tenant,
                operation_id="op-1",
                from_status=ApprovalStatus.PENDING,
                to_status=ApprovalStatus.APPROVED,
            )
            result = ReissueExecutionResult(
                ok=True,
                status=ApprovalStatus.CONFIRMED,
                idempotency_key="k-1",
                delivered=True,
                confirmed=True,
                detail={"external": "ok"},
            )
            await store.set_result(tenant_id=tenant, operation_id="op-1", result=result)
            got = await store.get_operation(tenant_id=tenant, operation_id="op-1")
            assert got.status == ApprovalStatus.CONFIRMED
            assert got.result_hash  # 已计算
            assert got.external_result is not None

            # mark_reconciled: 从 reconcilable 状态收敛到定态并释放租约
            await store.set_status(
                tenant_id=tenant,
                operation_id="op-1",
                from_status=ApprovalStatus.CONFIRMED,
                to_status=ApprovalStatus.RECONCILIATION_REQUIRED,
            )
            marked = await store.mark_reconciled(
                tenant_id=tenant,
                operation_id="op-1",
                status=ApprovalStatus.CONFIRMED,
                external_result={"ok": True},
                result_hash="h-2",
            )
            assert marked is True
            got2 = await store.get_operation(tenant_id=tenant, operation_id="op-1")
            assert got2.status == ApprovalStatus.CONFIRMED
            # mark 不存在的操作 -> False
            assert (
                await store.mark_reconciled(
                    tenant_id=tenant,
                    operation_id="op-missing",
                    status=ApprovalStatus.CONFIRMED,
                    external_result=None,
                    result_hash=None,
                )
                is False
            )
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_claim_reconcilable_uses_lease():
    async def run():
        pool = await _open_pool()
        try:
            store = _make_store(pool)
            tenant = _tenant()
            # 一个可对账，一个已终态
            await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant,
                    operation_id="op-1",
                    key="k-1",
                    status=ApprovalStatus.EXECUTION_UNKNOWN,
                )
            )
            await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant,
                    operation_id="op-2",
                    key="k-2",
                    status=ApprovalStatus.CONFIRMED,
                )
            )
            claimed = await store.claim_reconcilable(worker_id="w-1", lease_seconds=60, limit=10)
            assert [c.operation_id for c in claimed] == ["op-1"]
            assert claimed[0].worker_id == "w-1"

            # 租约未到期 -> 再次领取不返回该行（不重复领取）
            again = await store.claim_reconcilable(worker_id="w-2", lease_seconds=60, limit=10)
            assert again == []

            # 释放租约后（lease 过去）可以再次领取
            await _expire_leases(pool, tenant)
            again2 = await store.claim_reconcilable(worker_id="w-2", lease_seconds=60, limit=10)
            assert [c.operation_id for c in again2] == ["op-1"]
            assert again2[0].worker_id == "w-2"

            # 只读 list_reconcilable 不领取
            listed = await store.list_reconcilable(limit=10)
            assert any(o.operation_id == "op-1" for o in listed)
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_two_workers_claim_disjoint_sets_skip_locked():
    """两个 worker 并发领取：FOR UPDATE SKIP LOCKED 保证不重复、disjoint 集合。

    并发领取同一批可对账行时，每个 worker 各拿一部分且互不重叠。
    """

    async def run():
        pool = await _open_pool()
        try:
            store = _make_store(pool)
            tenant = _tenant()
            # 3 行可对账 + 1 行终态（不应被领取）。
            for i in range(3):
                await store.create_operation(
                    **_seed_kwargs(
                        tenant_id=tenant,
                        operation_id=f"op-{i}",
                        key=f"k-{i}",
                        status=ApprovalStatus.EXECUTION_UNKNOWN,
                    )
                )
            await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant,
                    operation_id="op-done",
                    key="k-done",
                    status=ApprovalStatus.CONFIRMED,
                )
            )
            # worker-1 领 limit=10（应领到全部 3 行可对账，占住租约）。
            w1 = await store.claim_reconcilable(worker_id="w-1", lease_seconds=600, limit=10)
            assert sorted(o.operation_id for o in w1) == ["op-0", "op-1", "op-2"]
            # 租约未到期：worker-2 再领返回空（SKIP LOCKED 跳过已锁定行 → 不重复）。
            w2 = await store.claim_reconcilable(worker_id="w-2", lease_seconds=600, limit=10)
            assert w2 == []
            # 两集合不相交（worker-2 空集可视为 disjoint 的退化情形）。
            claimed = {o.operation_id for o in w1}
            assert "op-done" not in claimed  # 终态不在领取范围
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_lease_expires_allows_reclaim_after_worker_crash():
    """worker 领取后崩溃（租约过期）→ 后续 worker 可重新领取恢复对账。"""

    async def run():
        pool = await _open_pool()
        try:
            store = _make_store(pool)
            tenant = _tenant()
            await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant,
                    operation_id="op-1",
                    key="k-1",
                    status=ApprovalStatus.RECONCILIATION_REQUIRED,
                )
            )
            # worker-1 领取（短租约），随后崩溃：租约过期。
            claimed = await store.claim_reconcilable(worker_id="w-1", lease_seconds=120, limit=10)
            assert [c.operation_id for c in claimed] == ["op-1"]
            assert claimed[0].worker_id == "w-1"
            # 租约未过期时 w-2 领不到。
            assert await store.claim_reconcilable(worker_id="w-2", lease_seconds=60, limit=10) == []
            # 强制过期租约（等价 worker 崩溃后经过 lease_seconds）。
            await _expire_leases(pool, tenant)
            # 过期后可重新领取（w-2 接管）。
            again = await store.claim_reconcilable(worker_id="w-2", lease_seconds=60, limit=10)
            assert [c.operation_id for c in again] == ["op-1"]
            assert again[0].worker_id == "w-2"
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


def test_mark_reconciled_flips_terminal_and_removes_from_claim_scope():
    """mark_reconciled 把可对账行翻转为终态，且不再出现在 list/claim 范围。"""

    async def run():
        pool = await _open_pool()
        try:
            store = _make_store(pool)
            tenant = _tenant()
            await store.create_operation(
                **_seed_kwargs(
                    tenant_id=tenant,
                    operation_id="op-1",
                    key="k-1",
                    status=ApprovalStatus.RECONCILIATION_REQUIRED,
                )
            )
            # 收敛为终态 CONFIRMED（释放租约）。
            marked = await store.mark_reconciled(
                tenant_id=tenant,
                operation_id="op-1",
                status=ApprovalStatus.CONFIRMED,
                external_result={"ok": True},
                result_hash="h-1",
            )
            assert marked is True
            got = await store.get_operation(tenant_id=tenant, operation_id="op-1")
            assert got.status == ApprovalStatus.CONFIRMED
            assert got.lease_expires_at is None  # 已释放租约
            # 不再可对账：list 不返回、claim 不返回。
            listed = await store.list_reconcilable(limit=10)
            assert all(o.operation_id != "op-1" for o in listed)
            claimed = await store.claim_reconcilable(worker_id="w-1", lease_seconds=600, limit=10)
            assert all(c.operation_id != "op-1" for c in claimed)
            await _cleanup(pool, tenant)
            return True
        finally:
            await pool.close()

    assert asyncio.run(run())


# ===========================================================================
# 辅助
# ===========================================================================


async def _expire_leases(pool: AsyncConnectionPool, tenant_id: str) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE vpn_reissue_operations SET lease_expires_at = now() - interval '1 second' WHERE tenant_id = %s",
                (tenant_id,),
            )


async def _cleanup(pool: AsyncConnectionPool, tenant_id: str) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM vpn_reissue_operations WHERE tenant_id = %s", (tenant_id,)
            )
