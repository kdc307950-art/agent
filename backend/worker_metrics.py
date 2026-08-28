"""跨进程 Worker 指标与心跳 —— 写入 DB，由 API 进程的 /metrics 与 /ready 聚合读取。

Worker（inbound/outbox/sla/recovery）是独立进程，进程内指标 API 读不到；
计数/时延写 worker_metrics，心跳写 worker_heartbeats，避免引入消息中间件。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# 直方图桶（秒）：与 Prometheus 默认兼容，P95 可由桶分布计算。
HISTOGRAM_BUCKETS = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

WORKER_TYPES = ("inbound", "outbox", "sla", "recovery")


def _labels_key(labels: dict[str, str] | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in (labels or {}).items()}


class WorkerMetricsDB:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def incr(self, metric: str, labels: dict[str, str] | None = None, amount: int | float = 1) -> None:
        labels = _labels_key(labels)
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO worker_metrics (metric, labels, value, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (metric, labels) DO UPDATE SET
                    value = worker_metrics.value + EXCLUDED.value,
                    updated_at = now()
                """,
                (metric, Jsonb(labels), amount),
            )

    async def observe(self, metric: str, value: float, labels: dict[str, str] | None = None) -> None:
        """记录直方图观测：更新对应桶计数 + _count + _sum。"""
        labels = _labels_key(labels)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                for bucket in HISTOGRAM_BUCKETS:
                    if value <= bucket:
                        await cursor.execute(
                            """
                            INSERT INTO worker_metrics (metric, labels, value, updated_at)
                            VALUES (%s, %s, 1, now())
                            ON CONFLICT (metric, labels) DO UPDATE SET
                                value = worker_metrics.value + 1, updated_at = now()
                            """,
                            (f"{metric}_bucket", Jsonb({**labels, "le": str(bucket)}),),
                        )
                await cursor.execute(
                    """
                    INSERT INTO worker_metrics (metric, labels, value, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (metric, labels) DO UPDATE SET
                        value = worker_metrics.value + EXCLUDED.value, updated_at = now()
                    """,
                    (f"{metric}_count", Jsonb(labels), 1),
                )
                await cursor.execute(
                    """
                    INSERT INTO worker_metrics (metric, labels, value, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (metric, labels) DO UPDATE SET
                        value = worker_metrics.value + EXCLUDED.value, updated_at = now()
                    """,
                    (f"{metric}_sum", Jsonb(labels), value),
                )

    async def beat(self, worker_type: str, worker_id: str) -> None:
        if worker_type not in WORKER_TYPES:
            raise ValueError(f"未知 worker 类型: {worker_type}")
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO worker_heartbeats (worker_type, worker_id, last_beat_at)
                VALUES (%s, %s, now())
                ON CONFLICT (worker_type, worker_id) DO UPDATE SET last_beat_at = now()
                """,
                (worker_type, worker_id),
            )

    @classmethod
    async def snapshot_metrics(cls, pool) -> list[dict[str, Any]]:
        async with pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT metric, labels, value, updated_at FROM worker_metrics ORDER BY metric, labels"
                )
                return list(await cursor.fetchall())

    @classmethod
    async def check_heartbeats(cls, pool, *, ttl_seconds: float = 90.0) -> dict[str, str]:
        """每类 worker 至少一个心跳在 TTL 内 => ok，否则 failed。"""
        async with pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT worker_type,
                           count(*) FILTER (WHERE last_beat_at > now() - (%s * interval '1 second')) AS fresh,
                           max(last_beat_at) AS latest
                    FROM worker_heartbeats
                    GROUP BY worker_type
                    """,
                    (ttl_seconds,),
                )
                rows = {row["worker_type"]: row for row in await cursor.fetchall()}
        return {
            worker_type: "ok" if rows.get(worker_type, {}).get("fresh", 0) >= 1 else "missing"
            for worker_type in WORKER_TYPES
        }

    @classmethod
    async def check_outbox_backlog(cls, pool) -> dict[str, int]:
        async with pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT
                        count(*) FILTER (WHERE status = 'pending' AND available_at <= now()) AS pending,
                        count(*) FILTER (WHERE status = 'dead') AS dead
                    FROM outbox_events
                    """
                )
                return dict(await cursor.fetchone())


def prometheus_text(rows: list[dict[str, Any]]) -> str:
    """把 worker_metrics 表转成 Prometheus 文本格式（计数/直方图）。"""
    lines: list[str] = []
    for row in rows:
        metric = row["metric"]
        labels = row["labels"] or {}
        if labels:
            rendered = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
            line = f"{metric}{{{rendered}}} {row['value']}"
        else:
            line = f"{metric} {row['value']}"
        lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def render_latency_quantile(rows: list[dict[str, Any]], metric: str) -> dict[str, float] | None:
    """从直方图桶估计 P95（线性插值）。"""
    count = sum(row["value"] for row in rows if row["metric"] == f"{metric}_count")
    if count <= 0:
        return None
    p95_index = 0.95 * count
    cumulative = 0.0
    buckets = {float(row["labels"]["le"]): row["value"] for row in rows if row["metric"] == f"{metric}_bucket"}
    for upper, bucket_count in sorted(buckets.items()):
        cumulative += bucket_count
        if cumulative >= p95_index:
            return {"count": int(count), "p95_seconds": upper}
    return {"count": int(count), "p95_seconds": None}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
