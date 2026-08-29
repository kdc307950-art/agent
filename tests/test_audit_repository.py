import asyncio
import os
from uuid import uuid4

import pytest

from backend.audit import audit_context
from backend.run_context import RunContext

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def _context(run_id: str, tenant_id: str = "tenant-a") -> RunContext:
    return RunContext(
        run_id=run_id,
        request_id=f"request-{run_id}",
        tenant_id=tenant_id,
        user_id="user-1",
        thread_id=f"{tenant_id}:user-1:thread-1",
        scopes=frozenset({"chat:read", "chat:write"}),
        deadline=asyncio.get_running_loop().time() + 60,
    )


def test_awaiting_approval_is_persisted_with_matching_event():
    run_id = f"awaiting-{uuid4().hex}"

    async def run():
        context = _context(run_id)
        async with audit_context(DATABASE_URL) as audit:
            await audit.setup()
            await audit.start_run(context)
            updated = await audit.finish_run(
                context,
                "awaiting_approval",
                metadata={"interrupt_id": "interrupt-1"},
            )
            stored = await audit.get_run("tenant-a", run_id)
            events = await audit.list_events("tenant-a", run_id)
        return updated, stored, events

    updated, stored, events = asyncio.run(run())
    assert updated is True
    assert stored is not None
    assert stored["status"] == "awaiting_approval"
    assert stored["finished_at"] is not None
    assert any(
        event["event_type"] == "run_awaiting_approval" and event["status"] == "awaiting_approval"
        for event in events
    )


def test_audit_round_trip_is_tenant_scoped_and_survives_reconnect():
    run_id = f"audit-{uuid4().hex}"

    async def run():
        context = _context(run_id)
        async with audit_context(DATABASE_URL) as audit:
            await audit.setup()
            await audit.start_run(context, metadata={"prompt": "must not be stored"})
            await audit.record_event(
                context,
                "tool_call_completed",
                tool_name="calculate",
                status="completed",
                payload={"input_chars": 5, "content": "secret"},
            )
            await audit.finish_run(context, "completed")
            own = await audit.get_run("tenant-a", run_id)
            own_events = await audit.list_events("tenant-a", run_id)
            foreign = await audit.get_run("tenant-b", run_id)
        async with audit_context(DATABASE_URL) as audit:
            persisted = await audit.get_run("tenant-a", run_id)
        return own, own_events, foreign, persisted

    own, own_events, foreign, persisted = asyncio.run(run())
    assert own is not None
    assert own["status"] == "completed"
    assert foreign is None
    assert persisted is not None
    assert any(event["event_type"] == "tool_call_completed" for event in own_events)
    assert all("secret" not in str(event["payload"]) for event in own_events)
    assert "must not be stored" not in str(own["metadata"])
