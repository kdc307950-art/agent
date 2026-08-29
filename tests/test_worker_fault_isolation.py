"""Worker 依赖故障注入测试（worker dependency fault-injection）—— 对应收敛方案阶段一验收项。

注意：指标/心跳"故障"通过替身对象（ExplodingMetrics）注入，验证 safe_* 隔离语义，
**不是**真实 PostgreSQL worker_metrics 表损坏/恢复测试；真实连接故障应使用隔离
连接、临时关闭写入或测试代理验证，不要通过破坏共享数据库完成。

覆盖：
- 指标/心跳写入失败（替身注入）不改变业务结果：入站仍 committed；
- 心跳失败不导致常驻进程退出；
- claim 阶段 DB 故障不终止 run_forever（单轮失败 → 记录 → 继续下一轮）；
- Outbox backlog 查询失败不终止循环，且不再假设 dead=0（不产生假 0 死信指标）。

依赖 DB 的用例单独标记 skipif；纯 fake 用例可在无 PostgreSQL 环境运行。
"""

import asyncio
import os

import pytest

from backend.inbound_worker import InboundWorker
from backend.outbox_worker import OutboxWorker

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


def _db_reachable() -> bool:
    """探测 TEST_DATABASE_URL 是否真实可达；不可达时 DB 用例应 skip 而非挂起。"""
    if not DATABASE_URL:
        return False
    import asyncio

    from psycopg import AsyncConnection

    async def probe() -> bool:
        try:
            connection = await AsyncConnection.connect(DATABASE_URL, connect_timeout=2)
            await connection.close()
            return True
        except Exception:
            return False

    return asyncio.run(probe())


class ExplodingMetrics:
    """WorkerMetricsDB 替身：所有写入抛异常，模拟指标/心跳写入依赖不可用（替身注入）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    async def incr(self, metric, labels=None, amount=1):
        self.calls.append(("incr", metric))
        raise RuntimeError("worker_metrics table unavailable")

    async def observe(self, metric, value, labels=None):
        self.calls.append(("observe", metric))
        raise RuntimeError("worker_metrics table unavailable")

    async def beat(self, worker_type, worker_id):
        self.calls.append(("beat", worker_type))
        raise RuntimeError("worker_heartbeats table unavailable")


class RecordingMetrics:
    """记录 safe_* 调用但不抛异常（验证指标被触发、且不干扰循环）。"""

    def __init__(self):
        self.incs: list[tuple[str, dict | None]] = []
        self.beats: list[str] = []

    async def incr(self, metric, labels=None, amount=1):
        self.incs.append((metric, labels))

    async def observe(self, metric, value, labels=None):
        pass

    async def beat(self, worker_type, worker_id):
        self.beats.append(worker_type)


class FlakyTickets:
    """claim 阶段模拟 DB 故障：前 N 次抛异常，之后正常返回空批次。"""

    def __init__(self, failures: int = 2):
        self.failures_left = failures
        self.claim_calls = 0

    async def claim_inbound_events(self, *, worker_id, lease_seconds, limit, tenant_id=None):
        self.claim_calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise RuntimeError("postgres connection refused")
        return []


class FakeRuntime:
    def __init__(self, tickets):
        self.tickets = tickets


class FakeOutboxRepository:
    def __init__(self):
        self.pool = object()

    async def claim_outbox(self, *, worker_id, lease_seconds, limit, tenant_id=None):
        return []

    async def renew_outbox_lease(self, tenant_id, event_id, *, worker_id, lease_seconds):
        return True

    async def complete_outbox(self, tenant_id, event_id, *, worker_id):
        return True

    async def fail_outbox(self, tenant_id, event_id, *, worker_id, error_code, retry_at):
        return True


class FlakyBacklogMetrics(RecordingMetrics):
    """backlog 查询失败（Outbox 循环内），但不影响 beat/incr 记录。"""

    async def check_outbox_backlog(self, pool):
        raise RuntimeError("backlog query failed")


async def _run_with_stop(coro_factory, *, seconds: float = 0.15):
    stop_event = asyncio.Event()

    async def stopper():
        await asyncio.sleep(seconds)
        stop_event.set()

    await asyncio.gather(coro_factory(stop_event), stopper())


# ---------- 不依赖 PostgreSQL 的隔离用例 ----------


def test_claim_failure_does_not_kill_run_forever():
    """claim 阶段 DB 故障：run_forever 不退出，记录 worker_loop_errors_total 后继续下一轮。"""
    tickets = FlakyTickets(failures=2)
    metrics = RecordingMetrics()
    worker = InboundWorker(FakeRuntime(tickets), batch_size=10, worker_metrics=metrics)

    async def run(stop_event):
        await worker.run_forever(poll_interval_seconds=0.01, stop_event=stop_event)

    asyncio.run(_run_with_stop(run))
    assert tickets.claim_calls >= 3  # 失败 2 轮 + 至少 1 轮成功
    assert ("worker_loop_errors_total", {"worker": "inbound"}) in metrics.incs
    assert metrics.beats.count("inbound") >= 1  # 恢复后心跳继续


def test_heartbeat_failure_does_not_kill_run_forever():
    """心跳写入失败：进程不退出，循环持续直到 stop_event。"""
    metrics = ExplodingMetrics()
    worker = InboundWorker(
        FakeRuntime(FlakyTickets(failures=0)), batch_size=10, worker_metrics=metrics
    )

    async def run(stop_event):
        await worker.run_forever(poll_interval_seconds=0.01, stop_event=stop_event)

    asyncio.run(_run_with_stop(run))  # 正常返回即证明未被心跳异常终止
    assert ("beat", "inbound") in metrics.calls


def test_outbox_backlog_query_failure_does_not_kill_loop_and_never_fake_zero():
    """backlog 查询失败：Outbox 循环继续，记录错误计数，且不触发 dead_present（不假 0）。"""
    metrics = FlakyBacklogMetrics()
    worker = OutboxWorker(FakeOutboxRepository(), {}, worker_metrics=metrics)

    async def run(stop_event):
        await worker.run_forever(poll_interval_seconds=0.01, stop_event=stop_event)

    asyncio.run(_run_with_stop(run))
    assert ("outbox_backlog_check_errors_total", None) in metrics.incs
    assert not any(metric == "outbox_dead_present_total" for metric, _ in metrics.incs)
    assert metrics.beats.count("outbox") >= 1


# ---------- 依赖 PostgreSQL 的隔离用例 ----------


@pytest.mark.skipif(not _db_reachable(), reason="TEST_DATABASE_URL 不可达（PostgreSQL 未运行）")
def test_metrics_failure_does_not_change_inbound_commit(monkeypatch):
    """指标库故障：入站事件仍 committed，工单已创建（safe_* 只记日志不改业务结果）。"""
    from backend.migrations import setup_postgres
    from backend.runtime import runtime_context
    from backend.seed_demo import _seed
    from backend.settings import Settings

    async def run():
        from uuid import uuid4

        event_id = f"evt-fault-{uuid4().hex}"
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            tenant = "tenant-fault-inject"
            await _seed(tenant, DATABASE_URL)
            await runtime.tickets.register_inbound_event(
                tenant,
                "wecom",
                event_id,
                {
                    "requester_id": "u1",
                    "external_ticket_id": None,
                    "title": "VPN 无法连接",
                    "content": "VPN 无法连接，错误码 809",
                    "channel": "wecom",
                    "raw": {},
                },
            )
            metrics = ExplodingMetrics()
            worker = InboundWorker(runtime, batch_size=10, tenant_id=tenant, worker_metrics=metrics)
            processed = await worker.run_once()
            row = await runtime.tickets.get_inbound_event(tenant, "wecom", event_id)
            return processed, row, metrics

    processed, row, metrics = asyncio.run(run())
    assert processed == 1
    assert row["status"] == "committed"
    assert row["ticket_id"] is not None
    # 指标/心跳写入确实被尝试过并失败（证明故障注入生效，而不是静默跳过打点）。
    assert any(call[0] == "incr" for call in metrics.calls)
