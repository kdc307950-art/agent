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
import math
from contextlib import suppress
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


class CopilotLeaseLost(RuntimeError):
    """当前 Worker 已失去运行租约，不能再写入业务结果。"""

    error_code = "copilot_lease_lost"


@dataclass(frozen=True, slots=True)
class CopilotWorkerRunResult:
    """单轮 Copilot Worker 处理统计。"""

    claimed: int = 0
    completed: int = 0
    retried: int = 0
    dead: int = 0
    recovered: int = 0
    lease_lost: int = 0


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
        interval = max(0.1, self.lease_seconds / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self.runtime.copilot_repository.renew_run_lease(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise CopilotLeaseLost("copilot_lease_lost") from exc
                if not renewed:
                    raise CopilotLeaseLost("copilot_lease_lost")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 续租失败：不再写入结果，标记失败（worker 轮询会按错误码处理）
            logger.warning("Copilot run %s 租约续期失败: %s", run_id, type(exc).__name__)
            raise

    async def _run_generation_with_lease(
        self,
        *,
        service,
        tenant_id: str,
        ticket_id: str,
        run_id: str,
        lease_task: asyncio.Task | None = None,
    ) -> dict:
        """让模型任务与续租任务竞速；租约失效时取消模型任务并拒绝结果。"""
        owns_lease_task = lease_task is None
        if lease_task is None:
            lease_task = asyncio.create_task(
                self._keep_lease_alive(tenant_id=tenant_id, run_id=run_id)
            )
        async def execute() -> dict:
            # 把身份快照读取也放入受租约监控的任务，避免领取后在上下文准备阶段
            # 阻塞过久而失去租约却仍开始模型调用。
            run_context = await self._build_run_context(
                tenant_id=tenant_id, ticket_id=ticket_id, run_id=run_id
            )
            return await service.run_with_tenant(
                runtime=self.runtime,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                run_context=run_context,
            )

        if lease_task.done():
            # 批量领取后排队期间可能已经失租；在创建模型协程前先检查，
            # 防止协程获得一个事件循环机会后产生副作用。
            try:
                lease_task.result()
            except asyncio.CancelledError as exc:
                raise CopilotLeaseLost("copilot_lease_lost") from exc
            except Exception as exc:
                if isinstance(exc, CopilotLeaseLost):
                    raise
                raise CopilotLeaseLost("copilot_lease_lost") from exc
            raise CopilotLeaseLost("copilot_lease_lost")

        work_task = asyncio.create_task(execute())
        try:
            done, _ = await asyncio.wait(
                {lease_task, work_task}, return_when=asyncio.FIRST_COMPLETED
            )
            # 同一调度点两个任务都可能完成；租约失败优先于模型结果。
            if lease_task in done:
                try:
                    lease_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    work_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await work_task
                    if isinstance(exc, CopilotLeaseLost):
                        raise
                    raise CopilotLeaseLost("copilot_lease_lost") from exc

            if work_task in done:
                # 续租任务可能与模型任务同时结束，仍需先检查其异常。
                if lease_task.done():
                    try:
                        lease_task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if isinstance(exc, CopilotLeaseLost):
                            raise
                        raise CopilotLeaseLost("copilot_lease_lost") from exc
                return work_task.result()

            raise CopilotLeaseLost("copilot_lease_lost")
        finally:
            if not work_task.done():
                work_task.cancel()
            if owns_lease_task and not lease_task.done():
                lease_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work_task
            if owns_lease_task:
                with suppress(asyncio.CancelledError, Exception):
                    await lease_task

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
        if run is None or run.get("ticket_id") != ticket_id:
            raise RuntimeError("copilot_run_context_mismatch")
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

    async def _process_run(self, run: dict, *, lease_task: asyncio.Task | None = None) -> None:
        """处理单条运行：生成 -> 门禁 -> 保存草稿 -> 完成。"""
        tenant_id = run["tenant_id"]
        ticket_id = run["ticket_id"]
        run_id = run["run_id"]
        service = self.runtime.copilot
        if service is None:
            raise RuntimeError("copilot_unavailable")

        started = monotonic()
        try:
            outcome = await self._run_generation_with_lease(
                service=service,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                run_id=run_id,
                lease_task=lease_task,
            )
            gated = outcome["result"]
        except CopilotLeaseLost:
            raise
        except Exception as exc:
            logger.exception("Copilot run %s 处理异常: %s", run_id, type(exc).__name__)
            raise RuntimeError("copilot_generation_failed") from exc

        latency_ms = int((monotonic() - started) * 1000)
        status = "completed" if gated.error_code is None else "failed"
        if status == "completed":
            draft_id = uuid4().hex
            committed = await self.runtime.copilot_repository.save_draft_and_complete_run(
                draft_id=draft_id,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                run_id=run_id,
                worker_id=self.worker_id,
                draft_answer=gated.draft_answer,
                steps=gated.troubleshooting_steps,
                citations=[c.model_dump(mode="json") for c in gated.citations],
                confidence=gated.confidence,
                needs_human_review=gated.needs_human_review,
                tool_calls=len(gated.tool_trace),
                latency_ms=latency_ms,
                retrieval_mode=gated.retrieval_mode,
                degraded=gated.degraded,
            )
            if not committed:
                raise CopilotLeaseLost("copilot_lease_lost")
            # 检索模式指标只在原子提交成功后上报。
            metrics = getattr(self.runtime, "metrics", None)
            if metrics is not None and gated.retrieval_mode:
                metrics.increment(
                    "copilot_retrieval_total", attributes={"mode": gated.retrieval_mode}
                )
                if gated.degraded:
                    metrics.increment("copilot_retrieval_degraded_total")
        else:
            # 门禁失败（无引用/伪造引用等）：按瞬时错误退避或致命处理
            raise RuntimeError(gated.error_code or "copilot_gate_failed")

    async def _handle_failure(self, run: dict, error_code: str) -> str:
        """处理失败；只有仍持租约并成功写库时才计 retry/dead。"""
        attempts = int(run.get("attempts") or 0)
        if error_code == "copilot_lease_lost":
            return "lease_lost"
        transient = error_code in TRANSIENT_ERROR_CODES
        if transient and attempts < self.max_attempts:
            retry_at = datetime.now(UTC) + timedelta(seconds=min(2 ** attempts, 30))
            updated = await self.runtime.copilot_repository.fail_copilot_run(
                tenant_id=run["tenant_id"],
                run_id=run["run_id"],
                worker_id=self.worker_id,
                error_code=error_code,
                retry_at=retry_at,
            )
            return "retried" if updated else "lease_lost"
        updated = await self.runtime.copilot_repository.fail_copilot_run(
            tenant_id=run["tenant_id"],
            run_id=run["run_id"],
            worker_id=self.worker_id,
            error_code=error_code,
            retry_at=None,
        )
        return "dead" if updated else "lease_lost"

    async def run_once(self, *, limit: int = 5) -> CopilotWorkerRunResult:
        """执行一轮：领取 -> 逐个处理 -> 恢复僵尸运行。"""
        metrics = getattr(self.runtime, "metrics", None)
        # 先恢复超租约僵尸运行（崩溃恢复），有批量限制
        recovered = await self.runtime.copilot_repository.recover_orphaned_runs(
            lease_seconds=self.lease_seconds, max_recover=20
        )
        if metrics is not None and recovered:
            metrics.increment("copilot_lease_recovered_total", recovered)
        if limit < 1 or limit > 20:
            raise ValueError("limit 必须在 1 到 20 之间")
        # 批量领取后立即为每条任务启动心跳；这样保留批量吞吐，同时避免
        # 尚未开始的任务在队列中等待时租约过期。
        runs = await self.runtime.copilot_repository.claim_copilot_runs(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            limit=limit,
        )
        claimed_count = len(runs)
        counts = {"completed": 0, "retried": 0, "dead": 0, "lease_lost": 0}
        lease_tasks = {
            run["run_id"]: asyncio.create_task(
                self._keep_lease_alive(tenant_id=run["tenant_id"], run_id=run["run_id"])
            )
            for run in runs
        }
        try:
            for run in runs:
                lease_task = lease_tasks[run["run_id"]]
                try:
                    await self._process_run(run, lease_task=lease_task)
                    counts["completed"] += 1
                except Exception as exc:
                    error_code = getattr(exc, "error_code", None) or str(exc) or "copilot_failed"
                    action = await self._handle_failure(run, error_code)
                    counts[action] += 1
                finally:
                    lease_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await lease_task
        finally:
            # 若领取/失败处理本身抛错，仍需清理尚未开始的租约监控任务，
            # 避免后台异常任务泄漏并在无 owner 时持续续租。
            for task in lease_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*lease_tasks.values(), return_exceptions=True)
        if metrics is not None:
            for status, count in counts.items():
                if count:
                    metrics.increment(f"copilot_{status}_total", count)
                    metrics.increment("copilot_runs_total", count, {"status": status})
        return CopilotWorkerRunResult(
            recovered=recovered,
            claimed=claimed_count,
            completed=counts["completed"],
            retried=counts["retried"],
            dead=counts["dead"],
            lease_lost=counts["lease_lost"],
        )

    async def run_forever(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        limit: int = 5,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """常驻循环：轮询领取并处理，直到 stop_event 置位。"""
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须为正数")
        while stop_event is None or not stop_event.is_set():
            try:
                await self.run_once(limit=limit)
            except Exception:
                logger.exception("Copilot Worker 单轮执行失败，继续下一轮")
            if self.worker_metrics is not None:
                await safe_beat(self.worker_metrics, "copilot_worker", self.worker_id)
            await asyncio.sleep(poll_interval_seconds)
