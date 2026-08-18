from backend.metrics import RuntimeMetrics


def test_runtime_metrics_keeps_snapshot_compatibility_and_exports_prometheus():
    metrics = RuntimeMetrics(service_name="test-agent")
    try:
        metrics.increment("agent_runs_total", 2, {"outcome": "completed"})
        metrics.observe("agent_run_duration_seconds", 0.25, {"outcome": "completed"})

        assert metrics.snapshot() == {"agent_runs_total": 2}
        payload, content_type = metrics.prometheus_payload()
        assert b"agent_runs_total" in payload
        assert b"agent_run_duration_seconds" in payload
        assert "text/plain" in content_type
    finally:
        metrics.shutdown()
