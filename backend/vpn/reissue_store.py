"""重新下发 VPN 配置文件（reissue_vpn_config）的持久化存储抽象。

模块归属：backend/vpn。职责：把「审批式执行」的幂等/状态登记表从纯内存
``ReissueRegistry`` 抽象为可注入、可持久化的 ``ReissueStore`` 接口，使得：

    - 单元测试 / 无数据库环境：用 ``InMemoryReissueStore``（缺省，行为对齐
      现有 ``ReissueRegistry``，既有测试不破坏）；
    - 生产环境：用 ``PostgresReissueStore``（psycopg ``AsyncConnectionPool``），
      审批状态跨进程存活、多 worker 用 ``FOR UPDATE SKIP LOCKED`` 竞争领取，
      满足「重启不丢失 + 进程正确争抢」两个生产目标。

设计说明（对齐 backend/vpn/repository.py 的仓储风格）：
    - 租户隔离：所有查询方法显式接收 tenant_id，SQL 强制 tenant_id 过滤；
    - 条件更新：``set_status`` 用 ``UPDATE ... WHERE status=from_status`` 实现
      乐观 CAS 语义，返回结构化 ``ReissueTransitionResult`` 供调用方映射到
      200 / 409 / 503；
    - 竞争领取：``claim_reconcilable`` 用 ``FOR UPDATE SKIP LOCKED`` + 租约
      （worker_id / lease_expires_at），保证同一行不会被两个 worker 同时领取。

注意：本模块只提供「持久化存储抽象 + 两种实现」，不承载状态机/审计逻辑
（那些仍在 approval.py / reissue_service.py，属于 eng-core 的改造范围）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .approval import ApprovalStatus, ReissueExecutionResult, ReissueRegistry


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _result_hash_value(result: ReissueExecutionResult) -> str:
    """对一个执行结果做确定性哈希（供 workflow_operation result_hash 对齐）。"""
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str)


# ===========================================================================
# 转移结果（结构化枚举，供调用方映射 HTTP 状态）
# ===========================================================================


class ReissueTransitionResult(StrEnum):
    """``ReissueStore.set_status`` 的原子 CAS 转移结果。

    成员（供 eng-core / eng-api-tests 映射）:
        SUCCESS             —— 条件更新成功，已从 from_status 转为 to_status（200）；
        ALREADY_TARGET_STATE—— 当前已是 to_status（幂等成功，无需再改）（200）；
        ILLEGAL_TRANSITION  —— 当前状态既非 from_status 也非 to_status，属非法状态机路径（409）；
        VERSION_CONFLICT    —— 操作已被并发 worker 抢先推进/占据（乐观锁冲突）（409）；
        DB_ERROR            —— 记录不存在或底层持久化失败（503）。
    """

    SUCCESS = "success"
    ALREADY_TARGET_STATE = "already_target_state"
    ILLEGAL_TRANSITION = "illegal_transition"
    VERSION_CONFLICT = "version_conflict"
    DB_ERROR = "db_error"

    @property
    def http_status(self) -> int:
        return _TRANSITION_HTTP_STATUS[self]


_TRANSITION_HTTP_STATUS: dict[ReissueTransitionResult, int] = {
    ReissueTransitionResult.SUCCESS: 200,
    ReissueTransitionResult.ALREADY_TARGET_STATE: 200,
    ReissueTransitionResult.ILLEGAL_TRANSITION: 409,
    ReissueTransitionResult.VERSION_CONFLICT: 409,
    ReissueTransitionResult.DB_ERROR: 503,
}

# ===========================================================================
# 异常分类 taxonomy（阶段二「状态机 + 补偿机制」的统一归类，集中此处避免散落）
# ===========================================================================
#
# store.set_status 的条件 CAS 结果 / 外部结果分类 → 对外语义（HTTP 状态）：
#
#   store.set_status 结果（ReissueTransitionResult，见上）           对外语义
#   ─────────────────────────────────────────────────────────────  ────────────
#   ALREADY_TARGET_STATE                                           幂等成功（200）
#   ILLEGAL_TRANSITION                                              非法状态机跳转（409）
#   VERSION_CONFLICT                                                乐观锁版本冲突（409）
#   DB_ERROR                                                        数据库异常（503）
#
#   外部结果分类（approval.classify_reissue_outcome）              对外语义
#   ─────────────────────────────────────────────────────────────  ────────────
#   EXECUTION_UNKNOWN（超时/歧义错误码）                           外部结果未知 → 补偿对账
#   CONFIRMED / DELIVERED                                          外部成功 → operation_confirmed
#   FAILED                                                         外部明确失败 → operation_failed
#
# 注意：本表只做「归类映射」；各枚举成员的值（'' 字符串）已固定且正确，切勿改动。
# ===========================================================================


# ===========================================================================
# 领域模型
# ===========================================================================


@dataclass
class ReissueOperation:
    """一次 reissue 审批操作的持久化记录（覆盖 schema vpn_reissue_operations 全部列）。

    ``status`` 使用 ``ApprovalStatus``（状态值与审批状态机保持一致）；
    ``request_snapshot`` 为发起审批时的请求快照（供补偿对账重建上下文）。
    """

    tenant_id: str
    ticket_id: str
    operation_id: str
    idempotency_key: str
    status: ApprovalStatus
    request_snapshot: dict[str, Any] = field(default_factory=dict)
    approver_user_id: str | None = None
    external_request_id: str | None = None
    vendor_system: str | None = None
    vendor_task_id: int | None = None
    submission_state: str | None = None
    target_snapshot: dict[str, Any] = field(default_factory=dict)
    diff_snapshot: Any = None
    diff_hash: str | None = None
    desired_state_hash: str | None = None
    external_result: dict[str, Any] | None = None
    result_hash: str | None = None
    error_code: str | None = None
    retry_count: int = 0
    last_polled_at: datetime | None = None
    poll_attempts: int = 0
    expected_version: int = 0
    ticket_version: int | None = None
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ===========================================================================
# 存储抽象（eng-core / eng-api-tests 依赖；方法签名保持稳定）
# ===========================================================================


class ReissueStore(Protocol):
    """审批式执行动作的持久化存储接口。

    所有方法以 ``*`` 强制关键字参数，避免调用方误用位置参数造成租户穿透。
    """

    async def get_operation(
        self, *, tenant_id: str, operation_id: str
    ) -> ReissueOperation | None:
        """按 (tenant_id, operation_id) 精确查询单条操作。"""
        ...

    async def get_by_idempotency_key(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ReissueOperation | None:
        """按 (tenant_id, idempotency_key) 查询操作（幂等锚点）。"""
        ...

    async def create_operation(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        operation_id: str,
        idempotency_key: str,
        request_snapshot: dict[str, Any],
        status: ApprovalStatus,
        expected_version: int,
    ) -> ReissueOperation:
        """登记一条新操作（PENDING / 首次登记）；已存在的活跃操作则幂等返回。"""
        ...

    async def set_status(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        from_status: ApprovalStatus,
        to_status: ApprovalStatus,
        approver_user_id: str | None = None,
        error_code: str | None = None,
        external_result: dict[str, Any] | None = None,
        result_hash: str | None = None,
        retry_count_incr: bool = False,
    ) -> ReissueTransitionResult:
        """原子 CAS：`UPDATE ... SET status=to_status WHERE ... AND status=from_status`。

        返回结构化结果，调用方据此映射：ALREADY_TARGET_STATE(幂等成功) /
        ILLEGAL_TRANSITION(409) / VERSION_CONFLICT(409) / DB_ERROR(503) / SUCCESS。
        """
        ...

    async def set_result(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        result: ReissueExecutionResult,
    ) -> None:
        """把一次执行最终结果写回（status / external_result / result_hash / error_code）。"""
        ...

    async def claim_reconcilable(
        self, *, worker_id: str, lease_seconds: int, limit: int
    ) -> list[ReissueOperation]:
        """用 FOR UPDATE SKIP LOCKED 领取可对账操作（多 worker 无重复）。

        过滤 ``status IN ('execution_unknown','reconciliation_required')``，
        并写入 worker_id + lease_expires_at（租约）标记归属。
        """
        ...

    async def list_reconcilable(self, *, limit: int) -> list[ReissueOperation]:
        """只读列出可对账操作（不领取、不改状态）。"""
        ...

    async def mark_reconciled(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        status: ApprovalStatus,
        external_result: dict[str, Any] | None,
        result_hash: str | None,
    ) -> bool:
        """对账收敛后把操作推到定态；返回是否命中并更新。"""
        ...

    async def record_submission_observation(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        submission_state: str,
        vendor_task_id: int | None,
        external_result: dict[str, Any] | None = None,
    ) -> bool:
        """记录人工确认的外部提交观察，不直接伪造成功结果。"""
        ...


def _snapshot_fields(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = snapshot or {}
    target_snapshot = snapshot.get("target_snapshot") or {}
    return {
        "vendor_system": snapshot.get("vendor_system") or "fortimanager",
        "target_snapshot": target_snapshot if isinstance(target_snapshot, dict) else {},
        "diff_snapshot": snapshot.get("diff_snapshot"),
        "diff_hash": snapshot.get("diff_hash"),
        "desired_state_hash": snapshot.get("desired_state_hash"),
    }


def _external_fields(external_result: dict[str, Any] | None) -> dict[str, Any]:
    result = external_result or {}
    task_id = result.get("vendor_task_id")
    try:
        task_id = int(task_id) if task_id is not None else None
    except (TypeError, ValueError):
        task_id = None
    return {
        "vendor_system": result.get("vendor_system") or "fortimanager",
        "vendor_task_id": task_id,
        "external_request_id": result.get("external_request_id"),
        "submission_state": result.get("submission_state"),
    }


# ===========================================================================
# 实现一：内存存储（缺省，对齐现有 ReissueRegistry 行为）
# ===========================================================================


class InMemoryReissueStore:
    """基于内存的实现（缺省），行为对齐现有 ``ReissueRegistry``。

    通过 ``registry`` 参数可注入一个共享 ``ReissueRegistry``；缺省自建一个。
    所有状态写入都会同步到该 registry（``_status`` / ``_result`` / ``_request``），
    使既有内存登记表读路径（``get_status`` / ``get_result`` / ``scan_reconcilable``
    等）仍能观察到存储的最新状态，实现「存储抽象包裹当前 registry 行为」。
    """

    def __init__(self, registry: ReissueRegistry | None = None) -> None:
        self.registry = registry or ReissueRegistry()
        self._ops: dict[str, ReissueOperation] = {}

    # ---- 查询 ----

    async def get_operation(
        self, *, tenant_id: str, operation_id: str
    ) -> ReissueOperation | None:
        op = self._ops.get(operation_id)
        if op is None or op.tenant_id != tenant_id:
            return None
        return self._fresh(op)

    async def get_by_idempotency_key(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ReissueOperation | None:
        for op in self._ops.values():
            if op.tenant_id == tenant_id and op.idempotency_key == idempotency_key:
                return self._fresh(op)
        return None

    async def create_operation(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        operation_id: str,
        idempotency_key: str,
        request_snapshot: dict[str, Any],
        status: ApprovalStatus,
        expected_version: int,
    ) -> ReissueOperation:
        # 幂等：同 (tenant, idempotency_key) 已有活跃操作 -> 直接返回（不重复登记）。
        existing = await self.get_by_idempotency_key(
            tenant_id=tenant_id, idempotency_key=idempotency_key
        )
        if existing is not None and existing.status not in (
            ApprovalStatus.FAILED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.CANCELLED,
        ):
            return existing
        now = _utcnow()
        op = ReissueOperation(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            request_snapshot=dict(request_snapshot or {}),
            status=status,
            expected_version=expected_version,
            **_snapshot_fields(request_snapshot),
            created_at=now,
            updated_at=now,
        )
        self._ops[operation_id] = op
        self._sync_registry(op)
        return self._fresh(op)

    async def set_status(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        from_status: ApprovalStatus,
        to_status: ApprovalStatus,
        approver_user_id: str | None = None,
        error_code: str | None = None,
        external_result: dict[str, Any] | None = None,
        result_hash: str | None = None,
        retry_count_incr: bool = False,
    ) -> ReissueTransitionResult:
        op = self._ops.get(operation_id)
        if op is None or op.tenant_id != tenant_id:
            return ReissueTransitionResult.DB_ERROR
        if op.status == to_status:
            # 已是目标状态 -> 幂等成功（不改写）。可选元数据在此无需强制回填。
            return ReissueTransitionResult.ALREADY_TARGET_STATE
        if op.status != from_status:
            # 当前既非 from 也非 to：并发推进中 -> VERSION_CONFLICT；否则非法路径。
            if op.status in (
                ApprovalStatus.EXECUTING,
                ApprovalStatus.EXECUTION_UNKNOWN,
                ApprovalStatus.RECONCILIATION_REQUIRED,
            ):
                return ReissueTransitionResult.VERSION_CONFLICT
            return ReissueTransitionResult.ILLEGAL_TRANSITION
        op.status = to_status
        if approver_user_id is not None:
            op.approver_user_id = approver_user_id
        if error_code is not None:
            op.error_code = error_code
        if external_result is not None:
            op.external_result = external_result
            external_fields = _external_fields(external_result)
            op.vendor_system = external_fields["vendor_system"]
            op.vendor_task_id = external_fields["vendor_task_id"]
            op.external_request_id = external_fields["external_request_id"]
            op.submission_state = external_fields["submission_state"]
        if result_hash is not None:
            op.result_hash = result_hash
        if retry_count_incr:
            op.retry_count += 1
        op.updated_at = _utcnow()
        self._sync_registry(op)
        return ReissueTransitionResult.SUCCESS

    async def set_result(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        result: ReissueExecutionResult,
    ) -> None:
        op = self._ops.get(operation_id)
        if op is None or op.tenant_id != tenant_id:
            return
        op.status = result.status
        op.external_result = result.model_dump(mode="json")
        external_fields = _external_fields(op.external_result)
        op.vendor_system = external_fields["vendor_system"]
        op.vendor_task_id = external_fields["vendor_task_id"]
        op.external_request_id = external_fields["external_request_id"]
        op.submission_state = external_fields["submission_state"]
        op.result_hash = _result_hash_value(result)
        op.error_code = result.error_code
        op.updated_at = _utcnow()
        self._sync_registry(op)
        self.registry._result[op.idempotency_key] = result

    async def claim_reconcilable(
        self, *, worker_id: str, lease_seconds: int, limit: int
    ) -> list[ReissueOperation]:
        claimed: list[ReissueOperation] = []
        for op in self._ops.values():
            if op.status not in (
                ApprovalStatus.EXECUTION_UNKNOWN,
                ApprovalStatus.RECONCILIATION_REQUIRED,
            ):
                continue
            if len(claimed) >= limit:
                break
            op.worker_id = worker_id
            op.retry_count += 1
            op.updated_at = _utcnow()
            claimed.append(self._fresh(op))
            self._sync_registry(op)
        return claimed

    async def list_reconcilable(self, *, limit: int) -> list[ReissueOperation]:
        out = [
            self._fresh(op)
            for op in self._ops.values()
            if op.status
            in (
                ApprovalStatus.EXECUTION_UNKNOWN,
                ApprovalStatus.RECONCILIATION_REQUIRED,
            )
        ]
        return out[:limit]

    async def mark_reconciled(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        status: ApprovalStatus,
        external_result: dict[str, Any] | None,
        result_hash: str | None,
    ) -> bool:
        op = self._ops.get(operation_id)
        if op is None or op.tenant_id != tenant_id:
            return False
        op.status = status
        if external_result is not None:
            op.external_result = external_result
        if result_hash is not None:
            op.result_hash = result_hash
        op.updated_at = _utcnow()
        self._sync_registry(op)
        return True

    async def record_submission_observation(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        submission_state: str,
        vendor_task_id: int | None,
        external_result: dict[str, Any] | None = None,
    ) -> bool:
        op = self._ops.get(operation_id)
        if op is None or op.tenant_id != tenant_id:
            return False
        op.submission_state = submission_state
        op.vendor_task_id = vendor_task_id
        op.vendor_system = (external_result or {}).get("vendor_system") or op.vendor_system or "fortimanager"
        if external_result is not None:
            op.external_result = {**(op.external_result or {}), **external_result}
            op.external_request_id = external_result.get("external_request_id") or op.external_request_id
        op.updated_at = _utcnow()
        self._sync_registry(op)
        return True

    # ---- 兼容读路径（供既有 registry 读接口适应） ----

    def get_status(self, idempotency_key: str) -> ApprovalStatus | None:
        return self.registry.get_status(idempotency_key)

    def get_result(self, idempotency_key: str) -> ReissueExecutionResult | None:
        return self.registry.get_result(idempotency_key)

    def get_request(self, idempotency_key: str) -> dict[str, Any] | None:
        return self.registry.get_request(idempotency_key)

    def scan_reconcilable(self) -> list[str]:
        return self.registry.scan_reconcilable()

    # ---- 内部 ----

    def _fresh(self, op: ReissueOperation) -> ReissueOperation:
        # 返回一个与存储解耦的拷贝，避免调用方意外改到内状态。
        return ReissueOperation(**{**op.__dict__})

    def _sync_registry(self, op: ReissueOperation) -> None:
        """把存储操作状态写回被包裹 ReissueRegistry，保持既有读路径一致。"""
        self.registry._status[op.idempotency_key] = op.status
        self.registry._request[op.idempotency_key] = dict(op.request_snapshot)


# ===========================================================================
# 实现二：PostgreSQL 存储（psycopg AsyncConnectionPool）
# ===========================================================================


class PostgresReissueStore:
    """PostgreSQL 持久化实现（psycopg ``AsyncConnectionPool``）。

    租户隔离：所有读/写强制 tenant_id 过滤。
    原子转移：``set_status`` 用条件 UPDATE（CAS）；``claim_reconcilable`` 用
    ``FOR UPDATE SKIP LOCKED`` + 租约（worker_id / lease_expires_at）。
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def get_operation(
        self, *, tenant_id: str, operation_id: str
    ) -> ReissueOperation | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_reissue_operations
                    WHERE tenant_id = %s AND operation_id = %s
                    """,
                    (tenant_id, operation_id),
                )
                row = await cursor.fetchone()
        return None if row is None else _row_to_operation(row)

    async def get_by_idempotency_key(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ReissueOperation | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_reissue_operations
                    WHERE tenant_id = %s AND idempotency_key = %s
                    ORDER BY created_at DESC, operation_id DESC
                    LIMIT 1
                    """,
                    (tenant_id, idempotency_key),
                )
                row = await cursor.fetchone()
        return None if row is None else _row_to_operation(row)

    async def create_operation(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        operation_id: str,
        idempotency_key: str,
        request_snapshot: dict[str, Any],
        status: ApprovalStatus,
        expected_version: int,
    ) -> ReissueOperation:
        try:
            async with self.pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO vpn_reissue_operations (
                            tenant_id, ticket_id, operation_id, idempotency_key,
                            request_snapshot, status, approver_user_id, external_request_id,
                            vendor_system, vendor_task_id, submission_state,
                            target_snapshot, diff_snapshot, diff_hash, desired_state_hash,
                            external_result, result_hash, error_code, retry_count,
                            expected_version, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            now(), now()
                        )
                        RETURNING *
                        """,
                        (
                            tenant_id,
                            ticket_id,
                            operation_id,
                            idempotency_key,
                            Jsonb(request_snapshot or {}),
                            status.value,
                            None,
                            None,
                            _snapshot_fields(request_snapshot)["vendor_system"],
                            None,
                            None,
                            Jsonb(_snapshot_fields(request_snapshot)["target_snapshot"]),
                            Jsonb(_snapshot_fields(request_snapshot)["diff_snapshot"])
                            if _snapshot_fields(request_snapshot)["diff_snapshot"] is not None
                            else None,
                            _snapshot_fields(request_snapshot)["diff_hash"],
                            _snapshot_fields(request_snapshot)["desired_state_hash"],
                            None,
                            None,
                            None,
                            0,
                            expected_version,
                        ),
                    )
                    row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("创建 VPN 操作后未返回数据库记录")
            return _row_to_operation(row)
        except psycopg.errors.UniqueViolation:
            # 幂等键冲突：同 (tenant, idempotency_key) 已有活跃操作 -> 返回既有。
            existing = await self.get_by_idempotency_key(
                tenant_id=tenant_id, idempotency_key=idempotency_key
            )
            if existing is not None:
                return existing
            raise

    async def set_status(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        from_status: ApprovalStatus,
        to_status: ApprovalStatus,
        approver_user_id: str | None = None,
        error_code: str | None = None,
        external_result: dict[str, Any] | None = None,
        result_hash: str | None = None,
        retry_count_incr: bool = False,
    ) -> ReissueTransitionResult:
        retry_column = "retry_count + 1" if retry_count_incr else "retry_count"
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # 条件更新：仅当当前 status == from_status 才推进（CAS）。
                await cursor.execute(
                    f"""
                    UPDATE vpn_reissue_operations
                    SET status = %s,
                        approver_user_id = COALESCE(%s, approver_user_id),
                        error_code = COALESCE(%s, error_code),
                        external_result = COALESCE(%s, external_result),
                        vendor_system = COALESCE(%s, vendor_system),
                        vendor_task_id = COALESCE(%s, vendor_task_id),
                        submission_state = COALESCE(%s, submission_state),
                        result_hash = COALESCE(%s, result_hash),
                        retry_count = {retry_column},
                        updated_at = now()
                    WHERE tenant_id = %s AND operation_id = %s AND status = %s
                    """,
                    (
                        to_status.value,
                        approver_user_id,
                        error_code,
                        Jsonb(external_result) if external_result is not None else None,
                        _external_fields(external_result)["vendor_system"] if external_result is not None else None,
                        _external_fields(external_result)["vendor_task_id"] if external_result is not None else None,
                        _external_fields(external_result)["submission_state"] if external_result is not None else None,
                        result_hash,
                        tenant_id,
                        operation_id,
                        from_status.value,
                    ),
                )
                if cursor.rowcount == 1:
                    return ReissueTransitionResult.SUCCESS
                # rowcount=0：区分「已是目标状态」「并发抢先」「非法」「不存在」。
                await cursor.execute(
                    "SELECT status FROM vpn_reissue_operations WHERE tenant_id = %s AND operation_id = %s",
                    (tenant_id, operation_id),
                )
                row = await cursor.fetchone()
        if row is None:
            return ReissueTransitionResult.DB_ERROR
        current = ApprovalStatus(row["status"])
        if current == to_status:
            return ReissueTransitionResult.ALREADY_TARGET_STATE
        if current in (
            ApprovalStatus.EXECUTING,
            ApprovalStatus.EXECUTION_UNKNOWN,
            ApprovalStatus.RECONCILIATION_REQUIRED,
        ):
            return ReissueTransitionResult.VERSION_CONFLICT
        return ReissueTransitionResult.ILLEGAL_TRANSITION

    async def set_result(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        result: ReissueExecutionResult,
    ) -> None:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE vpn_reissue_operations
                    SET status = %s,
                        external_result = %s,
                        vendor_system = %s,
                        vendor_task_id = %s,
                        submission_state = %s,
                        result_hash = %s,
                        error_code = %s,
                        updated_at = now()
                    WHERE tenant_id = %s AND operation_id = %s
                    """,
                    (
                        result.status.value,
                        Jsonb(result.model_dump(mode="json")),
                        _external_fields(result.model_dump(mode="json"))["vendor_system"],
                        _external_fields(result.model_dump(mode="json"))["vendor_task_id"],
                        _external_fields(result.model_dump(mode="json"))["submission_state"],
                        _result_hash_value(result),
                        result.error_code,
                        tenant_id,
                        operation_id,
                    ),
                )

    async def claim_reconcilable(
        self, *, worker_id: str, lease_seconds: int, limit: int
    ) -> list[ReissueOperation]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # FOR UPDATE SKIP LOCKED：并发 worker 不会领取同一行（无重复）。
                await cursor.execute(
                    """
                    SELECT * FROM vpn_reissue_operations
                    WHERE status IN ('execution_unknown', 'reconciliation_required')
                      AND (lease_expires_at IS NULL OR lease_expires_at < now())
                    ORDER BY created_at, operation_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = await cursor.fetchall()
                claimed: list[ReissueOperation] = []
                for row in rows:
                    # UPDATE ... RETURNING *：把刚写入的 worker_id / retry_count /
                    # lease_expires_at 一并带回，避免返回「领取前」的陈旧快照。
                    await cursor.execute(
                        """
                        UPDATE vpn_reissue_operations
                        SET worker_id = %s,
                            lease_expires_at = now() + make_interval(secs => %s),
                            retry_count = retry_count + 1,
                            updated_at = now()
                        WHERE tenant_id = %s AND operation_id = %s
                        RETURNING *
                        """,
                        (worker_id, lease_seconds, row["tenant_id"], row["operation_id"]),
                    )
                    updated = await cursor.fetchone()
                    if updated is not None:
                        claimed.append(_row_to_operation(updated))
        return claimed

    async def list_reconcilable(self, *, limit: int) -> list[ReissueOperation]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_reissue_operations
                    WHERE status IN ('execution_unknown', 'reconciliation_required')
                    ORDER BY created_at, operation_id
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = await cursor.fetchall()
        return [_row_to_operation(row) for row in rows]

    async def mark_reconciled(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        status: ApprovalStatus,
        external_result: dict[str, Any] | None,
        result_hash: str | None,
    ) -> bool:
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE vpn_reissue_operations
                    SET status = %s,
                        external_result = %s,
                        result_hash = %s,
                        lease_expires_at = NULL,
                        updated_at = now()
                    WHERE tenant_id = %s AND operation_id = %s
                    """,
                    (
                        status.value,
                        Jsonb(external_result) if external_result is not None else None,
                        result_hash,
                        tenant_id,
                        operation_id,
                    ),
                )
                return cursor.rowcount >= 1

    async def record_submission_observation(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        submission_state: str,
        vendor_task_id: int | None,
        external_result: dict[str, Any] | None = None,
    ) -> bool:
        merged = dict(external_result or {})
        merged["submission_state"] = submission_state
        merged["vendor_task_id"] = vendor_task_id
        fields = _external_fields(merged)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE vpn_reissue_operations
                    SET vendor_system = COALESCE(%s, vendor_system),
                        vendor_task_id = %s,
                        submission_state = %s,
                        external_request_id = COALESCE(%s, external_request_id),
                        external_result = COALESCE(external_result, '{}'::jsonb) || %s::jsonb,
                        updated_at = now()
                    WHERE tenant_id = %s AND operation_id = %s
                    """,
                    (
                        fields["vendor_system"],
                        fields["vendor_task_id"],
                        submission_state,
                        fields["external_request_id"],
                        Jsonb(merged),
                        tenant_id,
                        operation_id,
                    ),
                )
                return cursor.rowcount >= 1


# ===========================================================================
# DB 行 -> 领域对象
# ===========================================================================


def _row_to_operation(row: dict[str, Any]) -> ReissueOperation:
    return ReissueOperation(
        tenant_id=row["tenant_id"],
        ticket_id=row["ticket_id"],
        operation_id=row["operation_id"],
        idempotency_key=row["idempotency_key"],
        request_snapshot=row.get("request_snapshot") or {},
        status=ApprovalStatus(row["status"]),
        approver_user_id=row.get("approver_user_id"),
        external_request_id=row.get("external_request_id"),
        vendor_system=row.get("vendor_system"),
        vendor_task_id=(int(row["vendor_task_id"]) if row.get("vendor_task_id") is not None else None),
        submission_state=row.get("submission_state"),
        target_snapshot=row.get("target_snapshot") or {},
        diff_snapshot=row.get("diff_snapshot"),
        diff_hash=row.get("diff_hash"),
        desired_state_hash=row.get("desired_state_hash"),
        external_result=row.get("external_result"),
        result_hash=row.get("result_hash"),
        error_code=row.get("error_code"),
        retry_count=int(row.get("retry_count") or 0),
        expected_version=int(row.get("expected_version") or 0),
        ticket_version=row.get("ticket_version"),
        worker_id=row.get("worker_id"),
        lease_expires_at=row.get("lease_expires_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
