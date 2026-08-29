"""Resolution Copilot HTTP API 测试 —— 权限 / 状态校验 / 幂等响应。

覆盖 PRD 后端 API 行为：
- 非坐席 scope 调用返回 403
- 工单状态非 assigned/in_progress 返回 409
- operation_id 幂等：重复调用返回已有草稿（idempotent_replay=True）
- 生成失败返回 502 可重试错误，不改变工单状态
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.copilot.api import copilot_router
from backend.security import make_tenant_token

SECRET = "test-tenant-secret"


class _FakeTicket:
    def __init__(self, ticket_id: str, status: str = "assigned", version: int = 1):
        self.ticket_id = ticket_id
        self.tenant_id = "tenant-a"
        self.requester_id = "customer-1"
        self.title = "VPN 无法连接"
        self.description = "客户端无法连接公司 VPN"
        self.status = SimpleNamespace(value=status)
        self.category = "it.vpn"
        self.asset_id = None
        self.version = version


class _FakeTickets:
    def __init__(self, ticket: _FakeTicket | None):
        self.ticket = ticket

    async def get(self, tenant_id, ticket_id):
        if self.ticket is None or tenant_id != self.ticket.tenant_id:
            return None
        return self.ticket


class _FakeOps:
    async def get_ticket_overview(self, tenant_id, ticket_id):
        return {"messages": []}


class _FakeKnowledge:
    async def lexical_search(self, principal, query, limit=10):
        return []


class _FakeRepo:
    def __init__(self):
        self.runs = {}
        self.drafts = {}
        self.next_draft_id = 0
        self.recover_calls = 0

    async def get_run_by_operation(self, tenant_id, ticket_id, operation_id):
        return self.runs.get((tenant_id, ticket_id, operation_id))

    async def start_run(self, *, run_id, tenant_id, ticket_id, operation_id, agent_name="resolution_copilot", lease_seconds=60):
        key = (tenant_id, ticket_id, operation_id)
        if key in self.runs:
            return False
        self.runs[key] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "ticket_id": ticket_id,
            "status": "queued",
            "error_code": None,
            "tool_calls": 0,
        }
        return True

    async def get_run(self, tenant_id, run_id):
        for record in self.runs.values():
            if record["run_id"] == run_id and record.get("tenant_id") == tenant_id:
                return record
        return None

    async def finish_run(self, *, run_id, tenant_id, status, tool_calls, latency_ms, error_code=None):
        for key, record in self.runs.items():
            if record["run_id"] == run_id and key[0] == tenant_id:
                record["status"] = status
                record["error_code"] = error_code
                record["tool_calls"] = tool_calls

    async def recover_expired_runs(self, *, lease_seconds=60, max_attempts=2, now=None):
        self.recover_calls += 1
        return 0

    async def recover_orphaned_runs(self, *, lease_seconds=60, max_recover=20, now=None):
        self.recover_calls += 1
        return 0

    async def save_draft(self, **kwargs):
        self.next_draft_id += 1
        self.drafts[kwargs["draft_id"]] = {
            "draft_id": kwargs["draft_id"],
            "tenant_id": kwargs["tenant_id"],
            "ticket_id": kwargs["ticket_id"],
            "run_id": kwargs["run_id"],
            "draft_answer": kwargs["draft_answer"],
            "steps": kwargs["steps"],
            "citations": kwargs["citations"],
            "confidence": kwargs["confidence"],
            "needs_human_review": kwargs["needs_human_review"],
            "status": "generated",
        }

    async def get_latest_draft(self, tenant_id, ticket_id):
        for record in reversed(list(self.drafts.values())):
            if record["tenant_id"] == tenant_id and record["ticket_id"] == ticket_id:
                return record
        return None

    async def get_draft_by_run(self, tenant_id, ticket_id, run_id):
        for record in reversed(list(self.drafts.values())):
            if (
                record["tenant_id"] == tenant_id
                and record["ticket_id"] == ticket_id
                and record["run_id"] == run_id
            ):
                return record
        return None

    async def approve_draft(self, *, tenant_id, draft_id, approved_by):
        record = self.drafts.get(draft_id)
        if record is None or record["status"] not in ("generated", "reviewing"):
            return False
        record["status"] = "approved"
        return True


class _FakeCopilotService:
    """桩 CopilotService：可编程返回结果或抛异常。"""

    def __init__(self):
        self.fail = False
        self.last_run = None
        self.last_run_context = None

    async def run_with_tenant(self, *, runtime, tenant_id, ticket_id, run_context=None):
        if self.fail:
            raise RuntimeError("model down")
        self.last_run = (tenant_id, ticket_id)
        self.last_run_context = run_context
        from backend.copilot.models import CopilotResult

        return {
            "request": None,
            "raw": {},
            "result": CopilotResult(
                draft_answer="请先检查网络连接",
                troubleshooting_steps=["检查网络", "重新导入 VPN"],
                citations=[],
                confidence=0.91,
                needs_human_review=False,
                reason_codes=["gate_passed"],
            ),
        }


def _make_app(ticket: _FakeTicket | None, service: _FakeCopilotService | None = None):
    from backend.metrics import RuntimeMetrics
    from backend.rate_limit import InMemoryRateLimiter

    app = FastAPI()
    service = service or _FakeCopilotService()
    runtime = SimpleNamespace(
        tickets=_FakeTickets(ticket),
        ticket_operations=_FakeOps(),
        knowledge=_FakeKnowledge(),
        copilot=service,
        copilot_repository=_FakeRepo(),
        audit=None,
    )
    app.state.runtime = runtime
    app.state.settings = SimpleNamespace(
        auth_mode="dev",
        tenant_token_secret=SECRET,
        redis_fail_mode="open",
        agent_run_timeout_seconds=60,
    )
    app.state.metrics = RuntimeMetrics()
    app.state.rate_limiter = InMemoryRateLimiter(capacity=1000)
    app.state.memory_rate_limiter = InMemoryRateLimiter(capacity=1000)
    app.include_router(copilot_router)
    return TestClient(app)


def _headers(scope: str = "ticket:agent"):
    token = make_tenant_token(
        tenant_id="tenant-a",
        user_id="agent-1",
        secret=SECRET,
        scopes=[scope],
    )
    return {"Authorization": f"Bearer {token}"}


def test_copilot_requires_agent_scope():
    client = _make_app(_FakeTicket("t-1"))
    resp = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": f"op-{uuid4().hex}", "expected_version": 1},
        headers=_headers(scope="ticket:customer"),
    )
    assert resp.status_code == 403


def test_copilot_rejects_non_processing_status():
    ticket = _FakeTicket("t-1", status="resolved")
    client = _make_app(ticket)
    resp = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": f"op-{uuid4().hex}", "expected_version": 1},
        headers=_headers(),
    )
    assert resp.status_code == 409
    assert "不支持" in resp.json()["detail"]


def test_copilot_returns_404_for_missing_ticket():
    client = _make_app(None)
    resp = client.post(
        "/tickets/missing/copilot",
        json={"operation_id": f"op-{uuid4().hex}", "expected_version": 1},
        headers=_headers(),
    )
    assert resp.status_code == 404


def test_copilot_generation_success_and_latest():
    """POST 只入队（202 queued）；Worker 完成后 GET 状态返回草稿。"""
    ticket = _FakeTicket("t-1")
    client = _make_app(ticket)
    op_id = f"op-{uuid4().hex}"
    resp = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": op_id, "expected_version": 1},
        headers=_headers(),
    )
    # 异步 Worker 化：POST 立即返回 202 + queued，不执行模型
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    run_id = body["run_id"]

    # 模拟 Worker 完成：保存草稿 + 标记 completed
    import asyncio

    repo = client.app.state.runtime.copilot_repository
    service = client.app.state.runtime.copilot
    async def worker_complete():
        await repo.save_draft(
            draft_id=f"draft-{uuid4().hex}",
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_id=run_id,
            draft_answer="请先检查网络连接",
            steps=["检查网络", "重新导入 VPN"],
            citations=[],
            confidence=0.91,
            needs_human_review=False,
        )
        await repo.finish_run(
            run_id=run_id, tenant_id="tenant-a", status="completed",
            tool_calls=2, latency_ms=100,
        )
    asyncio.run(worker_complete())

    # GET 状态：completed + 对应草稿（按 run_id 关联）
    status = client.get(f"/tickets/t-1/copilot/{run_id}", headers=_headers())
    assert status.status_code == 200
    sbody = status.json()
    assert sbody["status"] == "completed"
    assert sbody["draft"]["draft_answer"] == "请先检查网络连接"

    # GET latest 返回同一草稿
    latest = client.get("/tickets/t-1/copilot/latest", headers=_headers())
    assert latest.status_code == 200
    assert latest.json()["draft"]["draft_answer"] == "请先检查网络连接"


def test_copilot_operation_id_idempotent_replay():
    """相同 operation_id 重复 POST：queued 时返回 202 同 run；completed 后返回草稿。"""
    ticket = _FakeTicket("t-1")
    client = _make_app(ticket)
    op_id = f"op-{uuid4().hex}"
    first = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": op_id, "expected_version": 1},
        headers=_headers(),
    )
    # 仍 queued：第二次调用返回 202 + 同一 run_id（不重复入队）
    second = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": op_id, "expected_version": 1},
        headers=_headers(),
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["run_id"] == first.json()["run_id"]

    # 模拟 Worker 完成后再调用：completed -> 返回对应草稿
    import asyncio

    repo = client.app.state.runtime.copilot_repository
    run_id = first.json()["run_id"]
    async def worker_complete():
        await repo.save_draft(
            draft_id=f"draft-{uuid4().hex}",
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_id=run_id,
            draft_answer="请先检查网络连接",
            steps=[],
            citations=[],
            confidence=0.91,
            needs_human_review=False,
        )
        await repo.finish_run(
            run_id=run_id, tenant_id="tenant-a", status="completed",
            tool_calls=1, latency_ms=50,
        )
    asyncio.run(worker_complete())
    third = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": op_id, "expected_version": 1},
        headers=_headers(),
    )
    assert third.status_code == 200
    assert third.json()["idempotent_replay"] is True
    assert third.json()["draft"]["draft_answer"] == "请先检查网络连接"


def test_copilot_approve_draft():
    """审批草稿：Worker 完成后按 run_id 查 draft 再审批。"""
    ticket = _FakeTicket("t-1")
    client = _make_app(ticket)
    op_id = f"op-{uuid4().hex}"
    import asyncio

    gen = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": op_id, "expected_version": 1},
        headers=_headers(),
    )
    run_id = gen.json()["run_id"]
    repo = client.app.state.runtime.copilot_repository
    async def worker_complete():
        await repo.save_draft(
            draft_id="draft-approve-1",
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_id=run_id,
            draft_answer="草稿",
            steps=[],
            citations=[],
            confidence=0.9,
            needs_human_review=False,
        )
        await repo.finish_run(
            run_id=run_id, tenant_id="tenant-a", status="completed",
            tool_calls=1, latency_ms=50,
        )
    asyncio.run(worker_complete())

    status = client.get(f"/tickets/t-1/copilot/{run_id}", headers=_headers())
    draft_id = status.json()["draft_id"]
    approve = client.post(
        f"/tickets/t-1/copilot/{draft_id}/approve",
        json={"note": "已核对"},
        headers=_headers(),
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    # 重复审批返回 409
    again = client.post(
        f"/tickets/t-1/copilot/{draft_id}/approve",
        json={},
        headers=_headers(),
    )
    assert again.status_code == 409


# ========== 阶段二：未初始化返回 503 ==========


def test_copilot_unavailable_returns_503():
    """未配置模型时 runtime.copilot 为 None，POST/GET 返回 503 而非 502。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.metrics import RuntimeMetrics
    from backend.rate_limit import InMemoryRateLimiter
    from backend.copilot.api import copilot_router

    app = FastAPI()
    # runtime.copilot = None（未配置模型服务）
    app.state.runtime = SimpleNamespace(
        tickets=_FakeTickets(_FakeTicket("t-1")),
        ticket_operations=_FakeOps(),
        knowledge=_FakeKnowledge(),
        copilot=None,
        copilot_repository=None,
    )
    app.state.settings = SimpleNamespace(
        auth_mode="dev",
        tenant_token_secret=SECRET,
        redis_fail_mode="open",
        agent_run_timeout_seconds=60,
    )
    app.state.metrics = RuntimeMetrics()
    app.state.rate_limiter = InMemoryRateLimiter(capacity=1000)
    app.state.memory_rate_limiter = InMemoryRateLimiter(capacity=1000)
    app.include_router(copilot_router)
    client = TestClient(app)

    resp = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": f"op-{uuid4().hex}", "expected_version": 1},
        headers=_headers(),
    )
    assert resp.status_code == 503
    assert "未初始化" in resp.json()["detail"]

    latest = client.get("/tickets/t-1/copilot/latest", headers=_headers())
    assert latest.status_code == 503


# ========== 阶段三：运行状态机（running -> 202，failed 不返旧草稿） ==========


def test_copilot_running_returns_202():
    """operation_id 对应的 run 仍为 queued/processing：返回 202，不重复调用模型。"""
    ticket = _FakeTicket("t-1")
    client = _make_app(ticket)
    repo = client.app.state.runtime.copilot_repository
    op_id = f"op-{uuid4().hex}"
    run_id = f"run-{uuid4().hex}"
    # 预置一条 queued 运行（模拟第一次请求已入队，Worker 尚未处理）
    import asyncio

    async def seed():
        await repo.start_run(
            run_id=run_id,
            tenant_id="tenant-a",
            ticket_id="t-1",
            operation_id=op_id,
        )

    asyncio.run(seed())
    resp = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": op_id, "expected_version": 1},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["run_id"] == run_id


def test_copilot_failed_run_does_not_return_stale_draft():
    """failed 运行不能当成功幂等结果：返回 409，不返回其他 run 的旧草稿。"""
    ticket = _FakeTicket("t-1")
    client = _make_app(ticket)
    repo = client.app.state.runtime.copilot_repository

    async def seed_with_fixed_op():
        old_run = f"run-old-{uuid4().hex}"
        await repo.start_run(
            run_id=old_run,
            tenant_id="tenant-a",
            ticket_id="t-1",
            operation_id="op-old-fixed",
        )
        await repo.finish_run(
            run_id=old_run, tenant_id="tenant-a", status="completed", tool_calls=1, latency_ms=10
        )
        await repo.save_draft(
            draft_id=f"draft-old-{uuid4().hex}",
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_id=old_run,
            draft_answer="旧草稿内容",
            steps=[],
            citations=[],
            confidence=0.9,
            needs_human_review=False,
        )
        failed_run = f"run-failed-{uuid4().hex}"
        await repo.start_run(
            run_id=failed_run,
            tenant_id="tenant-a",
            ticket_id="t-1",
            operation_id="op-failed-fixed",
        )
        await repo.finish_run(
            run_id=failed_run,
            tenant_id="tenant-a",
            status="failed",
            tool_calls=0,
            latency_ms=5,
            error_code="model_failed",
        )

    asyncio.run(seed_with_fixed_op())
    resp = client.post(
        "/tickets/t-1/copilot",
        json={"operation_id": "op-failed-fixed", "expected_version": 1},
        headers=_headers(),
    )
    assert resp.status_code == 409
    assert "上次 Copilot 生成失败" in resp.json()["detail"]
    assert "旧草稿内容" not in resp.json()["detail"]
