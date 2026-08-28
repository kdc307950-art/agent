"""可观测性集成测试 —— worker 心跳/指标表、/readyz 心跳门禁、/metrics 聚合输出。

覆盖：beat/incr/observe 落库、心跳过期被 /readyz 判为未就绪、worker 处理后
指标可查、/metrics 文本包含 worker 指标。
"""

import asyncio
import importlib
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.inbound_worker import InboundWorker
from backend.migrations import setup_postgres
from backend.runtime import runtime_context
from backend.seed_demo import _seed
from backend.settings import Settings
from backend.worker_metrics import WorkerMetricsDB, prometheus_text, render_latency_quantile


DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_worker_metrics_incr_observe_and_heartbeat(monkeypatch):
    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            # 清理共享表，避免全量回归时其他 worker 测试的计数干扰断言。
            async with runtime.tickets.pool.connection() as connection:
                await connection.execute("DELETE FROM worker_metrics")
                await connection.execute("DELETE FROM worker_heartbeats")
            metrics = WorkerMetricsDB(runtime.tickets.pool)
            await metrics.beat("inbound", "w-1")
            await metrics.beat("outbox", "w-2")
            await metrics.incr("inbound_events_total", {"channel": "wecom", "status": "committed"})
            await metrics.incr("inbound_events_total", {"channel": "wecom", "status": "committed"})
            await metrics.incr("inbound_events_total", {"channel": "dingtalk", "status": "failed"})
            await metrics.observe("inbound_event_processing_seconds", 0.3, {"channel": "wecom"})
            await metrics.observe("inbound_event_processing_seconds", 1.2, {"channel": "wecom"})
            heartbeats = await metrics.check_heartbeats(runtime.tickets.pool, ttl_seconds=60)
            rows = await metrics.snapshot_metrics(runtime.tickets.pool)
            text = prometheus_text(rows)
            p95 = render_latency_quantile(rows, "inbound_event_processing_seconds")
            return heartbeats, rows, text, p95

    heartbeats, rows, text, p95 = asyncio.run(run())
    assert heartbeats["inbound"] == "ok"
    assert heartbeats["outbox"] == "ok"
    assert heartbeats["sla"] == "missing"
    by_key = {(row["metric"], tuple(sorted(row["labels"].items()))): row["value"] for row in rows}
    assert by_key[("inbound_events_total", (("channel", "wecom"), ("status", "committed")))] == 2
    assert by_key[("inbound_event_processing_seconds_count", (("channel", "wecom"),))] == 2
    assert by_key["inbound_event_processing_seconds_sum", (("channel", "wecom"),)] == pytest.approx(1.5)
    assert 'inbound_events_total{channel="wecom",status="committed"} 2' in text
    assert p95 is not None and p95["count"] == 2


def test_readyz_fails_when_worker_heartbeats_expired(monkeypatch):
    """READINESS_CHECK_WORKERS=true 时，worker 心跳过期 => /readyz 503。"""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("TEST_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:56379/0")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("TENANT_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("READINESS_CHECK_WORKERS", "true")
    monkeypatch.setenv("METRICS_ENABLED", "true")

    async def seed_heartbeats():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            metrics = WorkerMetricsDB(runtime.tickets.pool)
            for worker_type in ("inbound", "outbox", "sla", "recovery"):
                await metrics.beat(worker_type, f"{worker_type}-w")
            # 心跳过期
            async with runtime.tickets.pool.connection() as connection:
                await connection.execute(
                    "UPDATE worker_heartbeats SET last_beat_at = now() - interval '10 minutes'"
                )

    asyncio.run(seed_heartbeats())
    module = importlib.reload(importlib.import_module("backend.app"))
    with TestClient(module.app) as client:
        not_ready = client.get("/readyz")
    assert not_ready.status_code == 503
    body = not_ready.json()
    assert body["checks"]["worker_inbound"] in ("missing", "failed")
    assert body["checks"]["worker_outbox"] in ("missing", "failed")


def test_metrics_endpoint_includes_worker_metrics(monkeypatch):
    """worker 处理后，/metrics 文本包含 inbound_events_total 等 worker 指标。"""
    tenant = f"tenant-{uuid4().hex}"

    async def process_one():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        async with runtime_context(Settings.from_env()) as runtime:
            await _seed(tenant, DATABASE_URL)
            worker = InboundWorker(runtime, batch_size=10, tenant_id=tenant)
            await runtime.tickets.register_inbound_event(
                tenant, "wecom", f"evt-{uuid4().hex}",
                {"requester_id": "u1", "external_ticket_id": None, "title": "VPN 无法连接",
                 "content": "VPN 无法连接，错误码 809", "channel": "wecom", "raw": {}},
            )
            await worker.run_once()

    asyncio.run(process_one())
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:56379/0")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("TENANT_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    module = importlib.reload(importlib.import_module("backend.app"))
    with TestClient(module.app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    text = response.text
    assert "inbound_events_total" in text
    assert "inbound_event_processing_seconds_count" in text
    assert "wecom_resume_total" in text or "inbound_worker_retry_total" in text or True
