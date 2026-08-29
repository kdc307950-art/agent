"""Copilot 异步 Worker —— 领取 queued 运行并执行 Copilot 生成（阶段二）。

职责：
    - 周期性领取 queued/failed 的 copilot_runs（FOR UPDATE SKIP LOCKED）
    - 对每条运行：准备上下文 -> 工具循环（治理）-> 引用门禁 -> 保存草稿
    - 成功标记 completed；瞬时错误指数退避（failed + next_attempt_at）；
      超过重试次数标记 dead
    - 定期 recover 超租约僵尸 processing 运行（崩溃恢复）

关键设计：
    - Web 进程（HTTP）不执行模型调用：POST 只创建 queued 运行返回 202，
      模型执行全部在 Worker 进程内完成
    - 租约互斥：多实例 Worker 依赖数据库租约，同一 run 只被一个实例领取
    - 心跳续租：长执行期间定期 renew_run_lease，崩溃后租约过期被 recover
    - 每轮失败只记录并退避，不终止进程
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

from ..run_context import RunContext
from ..worker_metrics import WorkerMetricsDB, safe_beat
from .tool_adapter import COPILOT_ALLOWED_TOOLS

logger = logging.getLogger("langgraph.copilot_worker")

# 瞬时错误码：可退避重试；其余视为致命（dead）
TRANSIENT_ERROR_CODES = {
    "copilot_timeout",
    "model_failed",
    "tool_call_limit_exceeded",
    "copilot_generation_failed",
}


@dataclass(frozen=True, slots=True)
class CopilotWorkerRunResult:
    """单轮 Copilot Worker 处理统计。"""

    claimed: int = 0
    completed: int = 0
    retried: int = 0
    dead: int = 0
    recovered: int = 0


class CopilotWorker:
    """Copilot 异步执行 Worker：领取 -> 生成 -> 门禁 -> 落库。"""

    def __init__(
        self,
        *,
        runtime,
        max_attempts: int = 3,
        lease_seconds: int = 60,
        worker_metrics: WorkerMetricsDB | None = None,
    ) -> None:
        self.runtime = runtime
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.worker_id = f"copilot-worker-{uuid4().hex[:8]}"
        self.worker_metrics = worker_metrics

    async def _lookup_agent_departments(self, tenant_id: str, user_id: str) -> frozenset[str]:
        """从服务端查询坐席所属部门（阶段一：部门身份透传）。

        来源是 support_members JOIN support_teams（服务端数据），
        请求体/模型不能提交部门；查询失败或非坐席返回空集合。
        """
        try:
            audit = getattr(self.runtime, "audit", None)
            pool = getattr(audit, "pool", None)
            if pool is None:
                return frozenset()
            async with pool.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT DISTINCT t.department_id
                        FROM support_members AS m
                        JOIN support_teams AS t
                          ON t.tenant_id = m.tenant_id AND t.team_id = m.team_id
                        WHERE m.tenant_id = %s AND m.member_id = %s AND m.active
                          AND t.department_id IS NOT NULL
                        """,
                        (tenant_id, user_id),
                    )
                    rows = await cursor.fetchall()
            return frozenset(str(row[0]) for row in rows if row and row[0])
        except Exception:
            return frozenset()

    async def _build_run_context(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        run_id: str,
    ) -> RunContext:
        """构造 Copilot 工具执行的服务端 RunContext（阶段一：身份透传）。

        部门由服务端查询填充；internal=True（客服工作台）；
        allowed_tools 限定 Copilot 只读 profile。
        已知边界：copilot_runs 当前未持久化 user_id（schema 扩展属 P2），
        异步 Worker 场景坐席部门透传暂为空集合（诚实标注，不伪造身份）。
        """
        departments = await self._lookup_agent_departments(tenant_id, "copilot-worker")
        return RunContext(
            run_id=run_id,
            request_id=f"copilot:{run_id}",
            tenant_id=tenant_id,
            user_id="copilot-worker",
            thread_id=f"copilot:{tenant_id}:{ticket_id}",
            scopes=frozenset({"ticket:agent"}),
            deadline=monotonic() + self.lease_seconds * 4,
            allowed_tools=frozenset(COPILOT_ALLOWED_TOOLS),
            role="agent",
            departments=departments,
            internal=True,
        )

    async def _process_run(self, run: dict) -> None:
        """处理单条运行：生成 -> 门禁 -> 保存草稿 -> 完成。"""
        tenant_id = run["tenant_id"]
        ticket_id = run["ticket_id"]
        run_id = run["run_id"]
        service = self.runtime.copilot
        if service is None:
            raise RuntimeError("copilot_unavailable")

        run_context = await self._build_run_context(
            tenant_id=tenant_id, ticket_id=ticket_id, run_id=run_id
        )
        started = monotonic()
        try:
            outcome = await service.run_with_tenant(
                runtime=self.runtime,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                run_context=run_context,
            )
            gated = outcome["result"]
        except Exception as exc:
            logger.exception("Copilot run %s 处理异常: %s", run_id, type(exc).__name__)
            raise RuntimeError("copilot_generation_failed") from exc

        latency_ms = int((monotonic() - started) * 1000)
        status = "completed" if gated.error_code is None else "failed"
        if status == "completed":
            draft_id = uuid4().hex
            await self.runtime.copilot_repository.save_draft(
                draft_id=draft_id,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                run_id=run_id,
                draft_answer=gated.draft_answer,
                steps=gated.troubleshooting_steps,
                citations=[c.model_dump(mode="json") for c in gated.citations],
                confidence=gated.confidence,
                needs_human_review=gated.needs_human_review,
            )
            await self.runtime.copilot_repository.complete_copilot_run(
                tenant_id=tenant_id,
                run_id=run_id,
                worker_id=self.worker_id,
                tool_calls=len(gated.tool_trace),
                latency_ms=latency_ms,
            )
        else:
            # 门禁失败（无引用/伪造引用等）：按瞬时错误退避或致命处理
            raise RuntimeError(gated.error_code or "copilot_gate_failed")

    async def _handle_failure(
        self, run: dict, error_code: str
    ) -> str:
        """处理失败：决定 retry / dead。返回 'retried' 或 'dead'。"""
        attempts = int(run.get("attempts") or 0)
        transient = error_code in TRANSIENT_ERROR_CODES
        if transient and attempts < self.max_attempts:
            retry_at = datetime.now(UTC) + timedelta(seconds=min(2 ** attempts, 30))
            await self.runtime.copilot_repository.fail_copilot_run(
                tenant_id=run["tenant_id"],
                run_id=run["run_id"],
                worker_id=self.worker_id,
                error_code=error_code,
                retry_at=retry_at,
            )
            return "retried"
        await self.runtime.copilot_repository.fail_copilot_run(
            tenant_id=run["tenant_id"],
            run_id=run["run_id"],
            worker_id=self.worker_id,
            error_code=error_code,
            retry_at=None,
        )
        return "dead"

    async def run_once(self, *, limit: int = 5) -> CopilotWorkerRunResult:
        """执行一轮：领取 -> 逐个处理 -> 恢复僵尸运行。"""
        metrics = getattr(self.runtime, "metrics", None)
        # 先恢复超租约僵尸运行（崩溃恢复），有批量限制
        recovered = await self.runtime.copilot_repository.recover_orphaned_runs(
            lease_seconds=self.lease_seconds, max_recover=20
        )
        if metrics is not None and recovered:
            metrics.increment("copilot_lease_recovered_total", recovered)
        runs = await self.runtime.copilot_repository.claim_copilot_runs(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            limit=limit,
        )
        counts = {"completed": 0, "retried": 0, "dead": 0}
        for run in runs:
            try:
                await self._process_run(run)
                counts["completed"] += 1
            except Exception as exc:
                error_code = str(exc) or "copilot_failed"
                action = await self._handle_failure(run, error_code)
                counts[action] += 1  # action in {"retried", "dead"}
        if metrics is not None:
            for status, count in counts.items():
                if count:
                    metrics.increment(f"copilot_{status}_total", count)
                    metrics.increment("copilot_runs_total", count, {"status": status})
        return CopilotWorkerRunResult(
            recovered=recovered,
            claimed=len(runs),
            completed=counts["completed"],
            retried=counts["retried"],
            dead=counts["dead"],
        )

    async def run_forever(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        limit: int = 5,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """常驻循环：轮询领取并处理，直到 stop_event 置位。"""
        while stop_event is None or not stop_event.is_set():
            try:
                await self.run_once(limit=limit)
            except Exception:
                logger.exception("Copilot Worker 单轮执行失败，继续下一轮")
            if self.worker_metrics is not None:
                await safe_beat(self.worker_metrics, "copilot_worker", self.worker_id)
            await asyncio.sleep(poll_interval_seconds)
