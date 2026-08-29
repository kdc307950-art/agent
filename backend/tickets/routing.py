"""工单路由规则与「最空闲 + 在岗」坐席选择。

职责：
    - 按租户配置的 routing_rules（优先级、分类/子分类/渠道/部门匹配）决定目标团队
    - 在目标团队内按「当前负载最少的在岗成员」选择具体坐席（least-loaded）
    - 找不到规则或没有可接单坐席时返回降级决策（回人工队列）

关键设计：
    - 规则命中 + 坐席选择在同一个事务里完成，SELECT ... FOR UPDATE
      锁定规则行，避免并发建单拿到同一批坐席
    - 坐席筛选用子查询统计其名下 in_progress/assigned 工单数，并与 capacity 比较
    - FOR UPDATE OF m SKIP LOCKED：跳过正被其他事务锁定的坐席行，
      支持多 Worker 并发路由而不互相阻塞
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """一次路由决策的结果。

    team_id/member_id 为 None 表示未命中规则或无人可接（进入人工队列）；
    reason_codes 记录决策依据（routing_rule / high_risk_priority /
    least_loaded_on_duty / manual_queue_*），供审计与排障。
    """

    team_id: str | None
    member_id: str | None
    reason_codes: tuple[str, ...]


class RoutingRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def route(
        self,
        *,
        tenant_id: str,
        category: str,
        subcategory: str | None,
        channel: str,
        department_id: str | None,
        risk_level: str,
        now: datetime | None = None,
    ) -> RoutingDecision:
        """执行一次路由：命中规则 -> 选「最空闲且在岗」坐席。

        risk_level == "high" 时把 high_risk_priority 插到 reason_codes 最前，
        便于下游按高优工单单独处理；now 允许测试注入固定时间。
        """
        reference = now or datetime.now(UTC)
        async with self.pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM routing_rules
                    WHERE tenant_id = %s AND active
                      AND (category IS NULL OR category = %s)
                      AND (subcategory IS NULL OR subcategory = %s)
                      AND (channel IS NULL OR channel = %s)
                      AND (department_id IS NULL OR department_id = %s)
                    ORDER BY priority, rule_id
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (tenant_id, category, subcategory, channel, department_id),
                )
                rule = await cursor.fetchone()
                if rule is None:
                    # 无规则命中：交给人工队列，不在路由层强行兜底
                    return RoutingDecision(None, None, ("manual_queue_no_rule",))
                reasons = ["routing_rule"]
                if risk_level == "high":
                    reasons.insert(0, "high_risk_priority")
                await cursor.execute(
                    """
                    SELECT m.member_id,
                           (SELECT count(*) FROM tickets AS t
                            WHERE t.tenant_id = m.tenant_id
                              AND t.assigned_user_id = m.member_id
                              AND t.status IN ('assigned', 'in_progress')) AS current_load,
                           m.capacity
                    FROM support_members AS m
                    WHERE m.tenant_id = %s AND m.team_id = %s AND m.active
                      AND (%s::TEXT IS NULL OR %s = ANY(m.skills))
                      AND EXISTS (
                          SELECT 1 FROM support_schedules AS s
                          WHERE s.tenant_id = m.tenant_id AND s.member_id = m.member_id
                            AND s.starts_at <= %s AND s.ends_at > %s
                      )
                      AND (SELECT count(*) FROM tickets AS t
                           WHERE t.tenant_id = m.tenant_id
                             AND t.assigned_user_id = m.member_id
                             AND t.status IN ('assigned', 'in_progress')) < m.capacity
                    ORDER BY current_load, m.member_id
                    LIMIT 1
                    FOR UPDATE OF m SKIP LOCKED
                    """,
                    (
                        tenant_id,
                        rule["target_team_id"],
                        rule["required_skill"],
                        rule["required_skill"],
                        reference,
                        reference,
                    ),
                )
                member = await cursor.fetchone()
                if member is None:
                    # 团队存在但没有满足技能/排班/容量条件的在岗成员
                    return RoutingDecision(
                        rule["target_team_id"], None, tuple((*reasons, "manual_queue_no_capacity"))
                    )
                return RoutingDecision(
                    rule["target_team_id"],
                    member["member_id"],
                    tuple((*reasons, "least_loaded_on_duty")),
                )
