"""LANGGraph VPN 客户处置闭环 —— PostgreSQL 持久化仓储（阶段二，方案 A）。

模块归属：backend/vpn。职责：把 VpnDiagnosisRun / VpnCustomerAction /
VpnCustomerActionResult / VpnEscalation 写入 backend/schema.py v23 新增的
vpn_diagnosis_runs / vpn_customer_actions / vpn_customer_action_results /
vpn_escalations 表，并按租户隔离查询。

设计（对齐 backend/tickets/repository.py 模式）：
    - 租户隔离：所有方法显式接收 tenant_id / ticket_id，查询强制 tenant_id 过滤。
    - 幂等写：RUN / ACTION / RESULT 均以主键 (run_id / action_id / result_id) 做
      ON CONFLICT upsert；ACTION 状态在执行结果回填后由 update_action_status 推进。
    - 不承载状态机/审计：状态迁移与审计由 closed_loop.py 负责，本仓储只做读写。

说明：closed_loop.VpnClosedLoopService 在生产环境（runtime 提供 vpn_diagnosis_repo）
时会把领域对象镜像写入本仓储（落库），单测（fake runtime）缺省仍用 in-memory
DiagnosisRegistry，因此两条路径都可用，且不破坏既有「内存登记表可单测」的口径。
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .diagnosis import (
    CustomerActionStatus,
    VpnCustomerAction,
    VpnCustomerActionResult,
    VpnDiagnosisRun,
    VpnEscalation,
)


class VpnDiagnosisRepository:
    """VPN 客户处置闭环领域对象的 PostgreSQL 仓储。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    # ---- 诊断运行 ----

    async def save_run(self, run: VpnDiagnosisRun) -> VpnDiagnosisRun:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO vpn_diagnosis_runs (
                        run_id, tenant_id, ticket_id, fault, hypothesis, confidence,
                        evidence, ruled_out, next_action, reason_codes, status,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO UPDATE SET
                        fault = EXCLUDED.fault,
                        hypothesis = EXCLUDED.hypothesis,
                        confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence,
                        ruled_out = EXCLUDED.ruled_out,
                        next_action = EXCLUDED.next_action,
                        reason_codes = EXCLUDED.reason_codes,
                        status = EXCLUDED.status,
                        updated_at = now()
                    """,
                    (
                        run.run_id,
                        run.tenant_id,
                        run.ticket_id,
                        run.fault,
                        run.hypothesis,
                        run.confidence,
                        Jsonb([finding.model_dump(mode="json") for finding in run.evidence]),
                        list(run.ruled_out),
                        run.next_action,
                        list(run.reason_codes),
                        run.status.value,
                        run.created_at,
                        run.updated_at,
                    ),
                )
        return run

    async def list_runs(self, tenant_id: str, ticket_id: str) -> list[VpnDiagnosisRun]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_diagnosis_runs
                    WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY created_at, run_id
                    """,
                    (tenant_id, ticket_id),
                )
                rows = await cursor.fetchall()
        return [_row_to_run(row) for row in rows]

    async def get_latest_run(self, tenant_id: str, ticket_id: str) -> VpnDiagnosisRun | None:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_diagnosis_runs
                    WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT 1
                    """,
                    (tenant_id, ticket_id),
                )
                row = await cursor.fetchone()
        return None if row is None else _row_to_run(row)

    # ---- 排查步骤 ----

    async def add_action(self, action: VpnCustomerAction) -> VpnCustomerAction:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO vpn_customer_actions (
                        action_id, tenant_id, ticket_id, run_id, title, instruction,
                        expected_result, risk_level, requires_agent, status, ordinal,
                        created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (action_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        ordinal = EXCLUDED.ordinal
                    """,
                    (
                        action.action_id,
                        action.tenant_id,
                        action.ticket_id,
                        action.run_id,
                        action.title,
                        action.instruction,
                        action.expected_result,
                        action.risk_level.value,
                        action.requires_agent,
                        action.status.value,
                        action.order,
                        action.created_at,
                    ),
                )
        return action

    async def get_action(
        self, tenant_id: str, ticket_id: str, action_id: str
    ) -> VpnCustomerAction | None:
        """按 (tenant_id, ticket_id, action_id) 精确查询单条客户动作。

        仅凭 action_id 查询会跨越租户/工单读到他人动作，故强制三元组过滤；
        跨租户（或工单不匹配）时返回 None，与内存版 DiagnosisRegistry 语义一致。
        """
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_customer_actions
                    WHERE tenant_id = %s AND ticket_id = %s AND action_id = %s
                    """,
                    (tenant_id, ticket_id, action_id),
                )
                row = await cursor.fetchone()
        return None if row is None else _row_to_action(row)

    async def list_actions(self, tenant_id: str, ticket_id: str) -> list[VpnCustomerAction]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_customer_actions
                    WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY ordinal, created_at
                    """,
                    (tenant_id, ticket_id),
                )
                rows = await cursor.fetchall()
        return [_row_to_action(row) for row in rows]

    async def update_action_status(
        self,
        tenant_id: str,
        ticket_id: str,
        action_id: str,
        status: CustomerActionStatus,
    ) -> bool:
        """把某一租户/工单下的客户动作状态推进到 status。

        仅按 action_id 更新会跨租户误改他人动作，故用三元组限定；
        rowcount 为 0（该租户/工单下不存在该动作）视为失败，返回 False。
        """
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE vpn_customer_actions
                    SET status = %s
                    WHERE tenant_id = %s AND ticket_id = %s AND action_id = %s
                    """,
                    (status.value, tenant_id, ticket_id, action_id),
                )
                return cursor.rowcount == 1

    # ---- 客户结果 ----

    async def add_action_result(self, result: VpnCustomerActionResult) -> VpnCustomerActionResult:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO vpn_customer_action_results (
                        result_id, tenant_id, ticket_id, action_id, run_id,
                        result, evidence, details, submitted_by, submitted_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (result_id) DO UPDATE SET
                        result = EXCLUDED.result,
                        evidence = EXCLUDED.evidence,
                        details = EXCLUDED.details,
                        submitted_by = EXCLUDED.submitted_by
                    """,
                    (
                        result.result_id,
                        result.tenant_id,
                        result.ticket_id,
                        result.action_id,
                        result.run_id,
                        result.result,
                        Jsonb(result.evidence),
                        result.details,
                        result.submitted_by,
                        result.submitted_at,
                    ),
                )
        return result

    async def list_action_results(
        self, tenant_id: str, ticket_id: str
    ) -> list[VpnCustomerActionResult]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_customer_action_results
                    WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY submitted_at DESC, result_id DESC
                    """,
                    (tenant_id, ticket_id),
                )
                rows = await cursor.fetchall()
        return [_row_to_result(row) for row in rows]

    # ---- 升级记录 ----

    async def add_escalation(self, escalation: VpnEscalation) -> VpnEscalation:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO vpn_escalations (
                        escalation_id, tenant_id, ticket_id, run_id, reason,
                        reason_codes, target_queue, status, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (escalation_id) DO UPDATE SET
                        reason = EXCLUDED.reason,
                        reason_codes = EXCLUDED.reason_codes,
                        target_queue = EXCLUDED.target_queue,
                        status = EXCLUDED.status
                    """,
                    (
                        escalation.escalation_id,
                        escalation.tenant_id,
                        escalation.ticket_id,
                        escalation.run_id,
                        escalation.reason,
                        list(escalation.reason_codes),
                        escalation.target_queue,
                        escalation.status.value,
                        escalation.created_at,
                    ),
                )
        return escalation

    async def list_escalations(self, tenant_id: str, ticket_id: str) -> list[VpnEscalation]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM vpn_escalations
                    WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY created_at ASC, escalation_id ASC
                    """,
                    (tenant_id, ticket_id),
                )
                rows = await cursor.fetchall()
        return [_row_to_escalation(row) for row in rows]


# ===========================================================================
# 行 -> 领域对象（DB 列名与模型字段名对齐）
# ===========================================================================


def _row_to_run(row: dict[str, Any]) -> VpnDiagnosisRun:
    return VpnDiagnosisRun.model_validate(_normalise_row(row))


def _row_to_action(row: dict[str, Any]) -> VpnCustomerAction:
    data = dict(row)
    # DB 列 order 与模型字段 order 存在语义映射：DB 用 ordinal，模型用 order。
    data["order"] = data.pop("ordinal", data.get("order", 0))
    return VpnCustomerAction.model_validate(_normalise_row(data))


def _row_to_result(row: dict[str, Any]) -> VpnCustomerActionResult:
    return VpnCustomerActionResult.model_validate(_normalise_row(row))


def _row_to_escalation(row: dict[str, Any]) -> VpnEscalation:
    return VpnEscalation.model_validate(_normalise_row(row))


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    """把 DB 行字段规整为与模型字段一致的 dict（status/risk_level 转枚举可接受值）。

    DB 列 status / risk_level 是文本；模型用 StrEnum，pydantic v2 能把字符串枚举值
    直接校验为枚举，故无需手工转换；这里主要处理 evidence JSONB / 数组已被 psycopg
    还原为 Python 对象的情形（已天然满足模型）。
    """
    return dict(row)
