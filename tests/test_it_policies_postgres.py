import asyncio
import os
from datetime import time
from uuid import uuid4

import pytest

from backend.migrations import setup_postgres
from backend.tickets import ItPolicyNotFound, ItPolicyRepository, TicketRepository, UpsertItPolicy

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


async def _seed_sla_policy(tickets: TicketRepository, tenant_id: str, policy_id: str) -> None:
    async with tickets.pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO sla_policies (
                tenant_id, policy_id, name, timezone, business_days,
                work_start, work_end, first_response_minutes, resolution_minutes
            ) VALUES (%s, %s, 'IT SLA', 'UTC', %s, %s, %s, 30, 240)
            """,
            (tenant_id, policy_id, [0, 1, 2, 3, 4], time(9), time(18)),
        )


def test_it_policy_upsert_requires_existing_sla_policy_and_returns_sla_minutes(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        repository = ItPolicyRepository(tickets.pool)
        try:
            await _seed_sla_policy(tickets, tenant, "it-default-sla")
            policy = await repository.upsert(
                tenant,
                UpsertItPolicy(
                    category="it.vpn", policy_id="it-default-sla", default_priority="high"
                ),
            )
            fetched = await repository.get(tenant, "it.vpn")
            active = await repository.list_active(tenant)
            return policy, fetched, active
        finally:
            await tickets.close()

    policy, fetched, active = asyncio.run(run())
    assert policy.policy_id == "it-default-sla"
    assert fetched.first_response_minutes == 30
    assert fetched.resolution_minutes == 240
    assert [item.category for item in active] == ["it.vpn"]


def test_it_policy_rejects_unknown_sla_policy_reference(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        repository = ItPolicyRepository(tickets.pool)
        try:
            with pytest.raises(ItPolicyNotFound):
                await repository.upsert(
                    tenant,
                    UpsertItPolicy(category="it.vpn", policy_id="missing-sla"),
                )
        finally:
            await tickets.close()

    asyncio.run(run())
