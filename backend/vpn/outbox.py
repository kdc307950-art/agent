"""VPN 重新下发（reissue_vpn_config）的 Outbox 发布帮助。

模块归属：backend/vpn。职责：把「审批式执行」每个生命周期转换作为一条
``vpn_reissue`` 聚合事件投递到仓库**既有**的通用 Outbox 表 ``outbox_events``
（schema v6/v8，含 pending/processing/delivered/dead 四态 + attempts + available_at
+ worker_id + lease_expires_at 租约），复用 ``backend/outbox_worker.py`` 的通用消费
Worker（``OutboxWorker`` / ``TicketOperationsRepository`` 的 claim/complete/fail/
renew_outbox_lease），**不另建并行投递体系**。

设计说明：
    - 发布点（authoritative）：由 ``VpnReissueService`` 在每个生命周期转换后调用
      ``ReissueOutbox.publish``（保持 ``ReissueStore`` 纯存储、不耦合投递）。
    - 事件类型（goal 要求的 7 类）：
        operation_started   <- start_reissue_approval 置 PENDING
        operation_approved  <- approve_reissue 置 APPROVED
        execution_started   <- execute 进入 EXECUTING
        execution_unknown   <- 外部结果未知 EXECUTION_UNKNOWN
        operation_confirmed <- CONFIRMED / DELIVERED
        operation_failed    <- FAILED
        reconciled          <- reconcile 补偿收敛到定态
    - 幂等：``idempotency_key = operation_id:event_type``。PG 侧用
      ``ON CONFLICT (tenant_id, idempotency_key) DO NOTHING``；内存侧用同一键去重，
      保证同一操作同一事件类型最多入箱一次（对齐「每转换一事件」）。
    - 兼容两种环境：无数据库（单元测试 / 缺省）用 ``InMemoryReissueOutbox`` 记录事件流
      （供断言）；生产/集成用 ``PostgresReissueOutbox(pool)`` 真正写入 outbox_events。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("langgraph.vpn")


# ===========================================================================
# 事件类型（goal 要求，外部语义稳定）
# ===========================================================================


class ReissueEventType(StrEnum):
    """VPN reissue 对外投递的生命周期事件类型（语义对齐 goal 的 7 类）。"""

    OPERATION_STARTED = "operation_started"
    OPERATION_APPROVED = "operation_approved"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_UNKNOWN = "execution_unknown"
    OPERATION_CONFIRMED = "operation_confirmed"
    OPERATION_FAILED = "operation_failed"
    RECONCILED = "reconciled"


# 聚合类型（outbox_events.aggregate_type）；aggregate_id = operation_id。
AGGREGATE_TYPE = "vpn_reissue"


def reissue_idempotency_key(operation_id: str, event_type: str | ReissueEventType) -> str:
    """生成 Outbox 幂等键：``operation_id:event_type``（同一转换只投递一次）。"""
    return f"{operation_id}:{event_type.value if isinstance(event_type, ReissueEventType) else event_type}"


# ===========================================================================
# 发布接口 + 两种实现
# ===========================================================================


class ReissueOutbox(Protocol):
    """Outbox 发布接口：把一条 reissue 生命周期事件投递到发件箱。

    实现方须保证同一 (tenant_id, idempotency_key) 幂等（重复 publish 不重复入箱），
    返回是否本次新入箱。
    """

    async def publish(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        event_type: str | ReissueEventType,
        payload: dict[str, Any],
    ) -> bool: ...


@dataclass
class InMemoryReissueOutbox:
    """内存 Outbox（无数据库缺省 / 单元测试）。

    以 ``idempotency_key`` 去重，模拟 PG 侧 ``ON CONFLICT DO NOTHING``；
    ``events`` 保留插入顺序，供测试断言事件流。
    """

    events: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ordered(self) -> list[dict[str, Any]]:
        return list(self.events.values())

    async def publish(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        event_type: str | ReissueEventType,
        payload: dict[str, Any],
    ) -> bool:
        key = reissue_idempotency_key(operation_id, event_type)
        if key in self.events:
            return False
        self.events[key] = {
            "tenant_id": tenant_id,
            "operation_id": operation_id,
            "event_id": key,  # 内存版：event_id 与幂等键一致即稳定可读
            "event_type": event_type.value if isinstance(event_type, ReissueEventType) else event_type,
            "aggregate_type": AGGREGATE_TYPE,
            "aggregate_id": operation_id,
            "idempotency_key": key,
            "payload": dict(payload or {}),
        }
        return True

    def get(self, operation_id: str, event_type: str | ReissueEventType) -> dict[str, Any] | None:
        return self.events.get(reissue_idempotency_key(operation_id, event_type))

    def event_types(self, operation_id: str) -> list[str]:
        """返回某操作已发布的全部事件类型（按插入顺序）。"""
        if not operation_id:
            return []
        prefix = f"{operation_id}:"
        return [
            e["event_type"]
            for k, e in self.events.items()
            if k.startswith(prefix)
        ]


class PostgresReissueOutbox:
    """PostgreSQL Outbox 发布（生产）：写入 ``outbox_events`` 表。

    复用通用发件箱表 + 通用 ``OutboxWorker`` 消费。写失败会抛 ``psycopg`` 异常，
    由调用方（service 的 ``_publish``）捕获并记录为警告（不静默丢弃，不走样）。
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def publish(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        event_type: str | ReissueEventType,
        payload: dict[str, Any],
    ) -> bool:
        type_value = (
            event_type.value if isinstance(event_type, ReissueEventType) else str(event_type)
        )
        event_id = f"{operation_id}:{type_value}:{uuid.uuid4().hex}"
        idem_key = reissue_idempotency_key(operation_id, type_value)
        async with self.pool.connection() as connection:
            async with connection.cursor() as cursor:
                # 幂等：同 (tenant_id, idempotency_key) 已入箱则 DO NOTHING，返回 False。
                await cursor.execute(
                    """
                    INSERT INTO outbox_events (
                        tenant_id, event_id, idempotency_key, event_type,
                        aggregate_type, aggregate_id, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    """,
                    (
                        tenant_id,
                        event_id,
                        idem_key,
                        type_value,
                        AGGREGATE_TYPE,
                        operation_id,
                        Jsonb(dict(payload or {})),
                    ),
                )
                return cursor.rowcount == 1
