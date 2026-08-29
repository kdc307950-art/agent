import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.migrations import setup_postgres
from backend.tickets import RoutingRepository, TicketRepository

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_routing_selects_on_duty_skilled_least_loaded_member(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    now = datetime.now(UTC)

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        tickets = await TicketRepository.connect(DATABASE_URL)
        try:
            async with tickets.pool.connection() as connection:
                await connection.execute(
                    "INSERT INTO support_teams (tenant_id, team_id, name) VALUES (%s, 'team-it', 'IT')",
                    (tenant,),
                )
                for member in ("agent-a", "agent-b"):
                    await connection.execute(
                        "INSERT INTO support_members (tenant_id, member_id, team_id, skills, capacity) VALUES (%s, %s, 'team-it', %s, 2)",
                        (tenant, member, ["sso"]),
                    )
                    await connection.execute(
                        "INSERT INTO support_schedules (tenant_id, schedule_id, member_id, starts_at, ends_at) VALUES (%s, %s, %s, %s, %s)",
                        (
                            tenant,
                            f"schedule-{member}",
                            member,
                            now - timedelta(hours=1),
                            now + timedelta(hours=1),
                        ),
                    )
                await connection.execute(
                    "INSERT INTO routing_rules (tenant_id, rule_id, category, required_skill, target_team_id) VALUES (%s, 'it-sso', 'it', 'sso', 'team-it')",
                    (tenant,),
                )
            return await RoutingRepository(tickets.pool).route(
                tenant_id=tenant,
                category="it",
                subcategory="general",
                channel="web",
                department_id=None,
                risk_level="high",
                now=now,
            )
        finally:
            await tickets.close()

    decision = asyncio.run(run())
    assert decision.team_id == "team-it"
    assert decision.member_id == "agent-a"
    assert decision.reason_codes == (
        "high_risk_priority",
        "routing_rule",
        "least_loaded_on_duty",
    )
