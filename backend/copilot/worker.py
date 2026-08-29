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

    async def _keep_lease_alive(self, *, tenant_id: str, run_id: str) -> None:
        """长任务期间定期续租，防止被误判为僵尸运行。

        续租失败（租约被其他 Worker 接管）时提前抛错中止本任务，
        防止两个 Worker 同时完成同一任务。
        """
        interval = max(1.0, self.lease_seconds / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = await self.runtime.copilot_repository.renew_run_lease(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
                if not renewed:
                    raise RuntimeError("copilot_lease_lost")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 续租失败：不再写入结果，标记失败（worker 轮询会按错误码处理）
            logger.warning("Copilot run %s 租约续期失败: %s", run_id, type(exc).__name__)
            raise

    async def _build_run_context(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        run_id: str,
    ) -> RunContext:
        """从运行记录恢复发起人身份快照构造 RunContext（阶段一：部门透传）。

        身份在 POST 入队时由服务端查询并持久化（requester_user_id /
        requester_role / requester_departments / requester_internal），
        Worker 执行时恢复快照——任务执行期间权限变化不影响本任务。
        身份缺失（requester_user_id 为空）视为闭锁失败：抛错转 failed，
        不使用默认全权限身份。
        """
        run = await self.runtime.copilot_repository.get_run(tenant_id, run_id)
        requester_user_id = (run or {}).get("requester_user_id") or ""
        if not requester_user_id:
            # 身份缺失闭锁：不伪造身份，任务失败转人工
            raise RuntimeError("copilot_identity_missing")
        departments = frozenset((run or {}).get("requester_departments") or [])
        internal = bool((run or {}).get("requester_internal", True))
        role = (run or {}).get("requester_role")
        return RunContext(
            run_id=run_id,
            request_id=f"copilot:{run_id}",
            tenant_id=tenant_id,
            user_id=requester_user_id,
            thread_id=f"copilot:{tenant_id}:{ticket_id}",
            scopes=frozenset({"ticket:agent"}),
            deadline=monotonic() + self.lease_seconds * 4,
            allowed_tools=frozenset(COPILOT_ALLOWED_TOOLS),
            role=role,
            departments=departments,
            internal=internal,
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
        # 长任务期间定期续租：防止其他 Worker 认为本任务僵尸而重复领取
        lease_task = asyncio.create_task(
            self._keep_lease_alive(tenant_id=tenant_id, run_id=run_id)
        )
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
        finally:
            lease_task.cancel()
            try:
                await lease_task
            except (asyncio.CancelledError, Exception):
                pass

        latency_ms = int((monotonic() - started) * 1000)
        status = "completed" if gated.error_code is None else "failed"
        # 检索模式指标（阶段二）：copilot_retrieval_total{mode} / degraded
        metrics = getattr(self.runtime, "metrics", None)
        if metrics is not None and gated.retrieval_mode:
            metrics.increment(
                "copilot_retrieval_total", attributes={"mode": gated.retrieval_mode}
            )
            if gated.degraded:
                metrics.increment("copilot_retrieval_degraded_total")
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
                retrieval_mode=gated.retrieval_mode,
                degraded=gated.degraded,
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
