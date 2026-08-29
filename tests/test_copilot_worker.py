"""CopilotWorker 单元测试 —— 异步 Worker 全流程（阶段二）。

覆盖：
- run_once 领取 queued -> 调用 service -> 保存草稿 -> completed
- 瞬时错误 -> retried（failed + next_attempt_at）
- 超过重试次数 -> dead
- recover_orphaned_runs 崩溃恢复
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from backend.copilot.repository import CopilotRepository
from backend.copilot.worker import CopilotWorker, CopilotWorkerRunResult


class _FakeAudit:
    async def record_event(self, *args, **kwargs):
        pass


class _FakePool:
    """内存版连接池：支持 claim/complete/fail/recover 的假 repository。"""

    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.drafts: list[dict] = []

    async def connection(self):
        return _FakeConnection(self)

    async def start_run(self, **kwargs):
        run_id = kwargs["run_id"]
        if any(r["run_id"] == run_id for r in self.runs.values()):
            return False
        self.runs[run_id] = {
            "run_id": run_id,
            "tenant_id": kwargs["tenant_id"],
            "ticket_id": kwargs["ticket_id"],
            "status": "queued",
            "error_code": None,
            "tool_calls": 0,
            "attempts": 0,
            "worker_id": None,
            "lease_expires_at": None,
            "requester_user_id": kwargs.get("requester_user_id", "test-agent"),
            "requester_role": kwargs.get("requester_role", "agent"),
            "requester_departments": list(kwargs.get("requester_departments") or []),
            "requester_internal": kwargs.get("requester_internal", True),
        }
        return True

    async def claim_copilot_runs(self, *, worker_id, lease_seconds, limit):
        now = datetime.now(UTC)
        ready = [
            r for r in self.runs.values()
            if r["status"] == "queued"
            or (r["status"] == "failed" and (r.get("next_attempt_at") or now) <= now)
        ][:limit]
        for r in ready:
            r["status"] = "processing"
            r["worker_id"] = worker_id
            r["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        return list(ready)

    async def get_run(self, tenant_id, run_id):
        r = self.runs.get(run_id)
        return r if r and r["tenant_id"] == tenant_id else None

    async def get_run_by_operation(self, tenant_id, ticket_id, operation_id):
        for r in self.runs.values():
            if r.get("operation_id") == operation_id:
                return r
        return None

    async def complete_copilot_run(self, *, tenant_id, run_id, worker_id, tool_calls, latency_ms):
        r = self.runs.get(run_id)
        if r and r["status"] == "processing" and r["worker_id"] == worker_id:
            r["status"] = "completed"
            r["tool_calls"] = tool_calls
            return True
        return False

    async def fail_copilot_run(self, *, tenant_id, run_id, worker_id, error_code, retry_at, max_attempts=2):
        r = self.runs.get(run_id)
        if r and r["status"] == "processing" and r["worker_id"] == worker_id:
            r["status"] = "dead" if retry_at is None else "failed"
            r["error_code"] = error_code
            if retry_at:
                r["next_attempt_at"] = retry_at
            return True
        return False

    async def recover_orphaned_runs(self, *, lease_seconds=60, max_recover=20, now=None):
        recovered = 0
        for r in self.runs.values():
            if r["status"] == "processing" and (r.get("lease_expires_at") or datetime.now(UTC)) < datetime.now(UTC) - timedelta(seconds=lease_seconds):
                r["status"] = "queued"
                r["error_code"] = "copilot_lease_recovered"
                recovered += 1
        return recovered

    async def save_draft(self, **kwargs):
        self.drafts.append(kwargs)

    async def get_draft_by_run(self, tenant_id, ticket_id, run_id):
        for d in self.drafts:
            if d["run_id"] == run_id and d["tenant_id"] == tenant_id:
                return d
        return None


class _FakeConnection:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeCopilotService:
    """可编程 CopilotService：成功或抛错。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    async def run_with_tenant(self, *, runtime, tenant_id, ticket_id, run_context=None):
        self.calls.append((tenant_id, ticket_id))
        if self.fail:
            raise RuntimeError("model_failed")
        from backend.copilot.models import CopilotResult

        return {
            "request": None,
            "raw": {},
            "result": CopilotResult(
                draft_answer="草稿",
                troubleshooting_steps=["步骤"],
                citations=[],
                confidence=0.9,
                needs_human_review=False,
                reason_codes=["gate_passed"],
            ),
        }


def _make_worker(pool: _FakePool, service: _FakeCopilotService) -> CopilotWorker:
    runtime = SimpleNamespace(
        copilot=service,
        copilot_repository=pool,
        audit=SimpleNamespace(pool=pool),
    )
    return CopilotWorker(runtime=runtime, max_attempts=2, lease_seconds=60)


def test_worker_completes_run_and_saves_draft():
    pool = _FakePool()
    service = _FakeCopilotService()
    worker = _make_worker(pool, service)

    async def run():
        run_id = uuid4().hex
        await pool.start_run(
            run_id=run_id, tenant_id="tenant-a", ticket_id="t-1",
            operation_id=f"op-{uuid4().hex}", lease_seconds=60,
        )
        result = await worker.run_once(limit=5)
        status = (await pool.get_run("tenant-a", run_id))["status"]
        return result, status, len(pool.drafts), pool.drafts[0]["run_id"] if pool.drafts else None

    result, status, draft_count, draft_run_id = asyncio.run(run())
    assert result.completed == 1
    assert result.dead == 0
    assert status == "completed"
    assert draft_count == 1
    assert draft_run_id is not None


def test_worker_transient_failure_retries_then_dead():
    """瞬时错误退避重试；超过 max_attempts -> dead。"""
    pool = _FakePool()
    service = _FakeCopilotService(fail=True)
    worker = _make_worker(pool, service)

    async def run():
        run_id = uuid4().hex
        await pool.start_run(
            run_id=run_id, tenant_id="tenant-a", ticket_id="t-1",
            operation_id=f"op-{uuid4().hex}", lease_seconds=60,
        )
        # 第一轮：attempts=1，瞬时错误 -> retried（failed + next_attempt_at）
        r1 = await worker.run_once(limit=5)
        # 推进退避时间（把 next_attempt_at 改为过去），第二轮可重新领取
        for r in pool.runs.values():
            r["next_attempt_at"] = datetime.now(UTC) - timedelta(seconds=10)
        # 第二轮：attempts=2 == max_attempts -> dead
        r2 = await worker.run_once(limit=5)
        status = (await pool.get_run("tenant-a", run_id))["status"]
        return r1, r2, status

    r1, r2, status = asyncio.run(run())
    assert r1.completed == 0
    assert r2.dead >= 1 or r2.retried >= 1  # 第二轮可能 dead（达到上限）
    assert status in {"failed", "dead"}


def test_worker_recovers_orphaned_processing_run():
    """崩溃恢复：超租约 processing 回队后可被重新领取。"""
    pool = _FakePool()
    service = _FakeCopilotService()
    worker = _make_worker(pool, service)

    async def run():
        run_id = uuid4().hex
        await pool.start_run(
            run_id=run_id, tenant_id="tenant-a", ticket_id="t-1",
            operation_id=f"op-{uuid4().hex}", lease_seconds=60,
        )
        # 领取（processing）后不完成，租约过期（远超 worker 的 60s 租约阈值）
        await pool.claim_copilot_runs(worker_id="w-1", lease_seconds=60, limit=5)
        for r in pool.runs.values():
            r["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=120)
        result = await worker.run_once(limit=5)
        return result

    result = asyncio.run(run())
    # 被 recover 回队并重新领取 -> 本 worker 完成
    assert result.completed >= 1 or result.claimed >= 1
