import asyncio
import os
from uuid import uuid4

import pytest

from backend.audit import AuditRepository
from backend.migrations import setup_postgres

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_admin_audit_events_are_recorded_and_tenant_scoped(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    other = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        audit = await AuditRepository.connect(DATABASE_URL)
        try:
            await audit.record_admin_event(
                tenant_id=tenant,
                user_id="it-admin",
                action="asset.create",
                resource_type="asset",
                resource_id="asset-1",
                detail={"asset_no": "A-1"},
            )
            await audit.record_admin_event(
                tenant_id=other,
                user_id="it-admin",
                action="it_policy.upsert",
                resource_type="it_policy",
                resource_id="it.vpn",
            )
            events = await audit.list_admin_events(tenant)
            return events
        finally:
            await audit.close()

    events = asyncio.run(run())
    assert len(events) == 1
    assert events[0]["action"] == "asset.create"
    assert events[0]["resource_id"] == "asset-1"
    assert events[0]["tenant_id"] == tenant
    assert events[0]["detail"]["asset_no"] == "A-1"
