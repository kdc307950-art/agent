"""可信渠道身份目录 PostgreSQL 集成测试（Day 3-4）。"""

import asyncio
import os
from uuid import uuid4

import pytest

from backend.channel_identities import ChannelIdentityRepository, UpsertChannelIdentity
from backend.migrations import setup_postgres

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_channel_identity_directory_crud_and_unique_key(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        from backend.audit import audit_context

        async with audit_context(DATABASE_URL) as audit:
            repo = ChannelIdentityRepository(audit.pool)
            first = await repo.upsert(
                tenant,
                UpsertChannelIdentity(
                    channel="wecom",
                    requester_id="ext-user-1",
                    departments=["finance"],
                    asset_id="asset-1",
                    external_user_id="wecom-user-1",
                ),
            )
            second = await repo.upsert(
                tenant,
                UpsertChannelIdentity(
                    channel="wecom",
                    requester_id="ext-user-1",
                    departments=["it"],
                ),
            )
            got = await repo.get(tenant, "wecom", "ext-user-1")
            listed = await repo.list_admin(tenant)
            deleted = await repo.delete(tenant, "wecom", "ext-user-1")
            missing_after = await repo.get(tenant, "wecom", "ext-user-1")
            return first, second, got, listed, deleted, missing_after

    first, second, got, listed, deleted, missing_after = asyncio.run(run())
    assert first.departments == ("finance",)
    assert second.departments == ("it",)
    assert got is not None
    assert got.departments == ("it",)
    assert got.external_user_id == "wecom-user-1"
    assert len(listed) == 1
    assert deleted is True
    assert missing_after is None
