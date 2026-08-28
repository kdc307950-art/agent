import asyncio
import os
from uuid import uuid4

import psycopg
import pytest

from backend.assets import AssetAlreadyExists, AssetRepository
from backend.assets.models import AssetStatus, CreateAsset, UpdateAsset
from backend.migrations import setup_postgres
from backend.tickets import AssetBindingError, CreateTicket, TicketRepository
from src.my_agent.helpdesk import ActorType


DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def create_request(asset_id: str, asset_no: str) -> CreateAsset:
    return CreateAsset(
        asset_id=asset_id,
        asset_no=asset_no,
        asset_type="laptop",
        name="Work Laptop",
        hostname="host-a",
        owner_user_id="user-1",
        department="it",
    )


def test_asset_crud_is_tenant_scoped_and_clears_fields_on_explicit_null(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    other = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        repository = await AssetRepository.connect(DATABASE_URL)
        try:
            created = await repository.create(tenant, create_request(f"asset-{uuid4().hex}", "A-001"))
            foreign = await repository.get(other, created.asset_id)
            cleared = await repository.update(
                tenant, created.asset_id, UpdateAsset(hostname=None, ip_address="10.0.0.5")
            )
            return created, foreign, cleared
        finally:
            await repository.close()

    created, foreign, cleared = asyncio.run(run())
    assert foreign is None
    assert cleared.hostname is None
    assert cleared.ip_address == "10.0.0.5"
    assert created.hostname == "host-a"


def test_asset_unique_violation_is_reported_and_soft_deleted_no_reusable(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    asset_no = "A-DUP-001"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        repository = await AssetRepository.connect(DATABASE_URL)
        try:
            first = await repository.create(tenant, create_request(f"asset-{uuid4().hex}", asset_no))
            with pytest.raises(AssetAlreadyExists):
                await repository.create(tenant, create_request(f"asset-{uuid4().hex}", asset_no))
            assert await repository.soft_delete(tenant, first.asset_id) is True
            assert await repository.get(tenant, first.asset_id) is None
            rebuilt = await repository.create(tenant, create_request(f"asset-{uuid4().hex}", asset_no))
            return rebuilt
        finally:
            await repository.close()

    rebuilt = asyncio.run(run())
    assert rebuilt.asset_no == asset_no


def test_ticket_asset_composite_fk_is_tenant_scoped(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"
    other = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        assets = await AssetRepository.connect(DATABASE_URL)
        tickets = await TicketRepository.connect(DATABASE_URL)
        try:
            asset = await assets.create(tenant, create_request(f"asset-{uuid4().hex}", "A-FK-001"))
            await tickets.create(
                tenant,
                CreateTicket(
                    ticket_id=f"ticket-{uuid4().hex}",
                    requester_id="user-1",
                    channel="web",
                    title="VPN issue",
                    actor_type=ActorType.CUSTOMER,
                    actor_id="user-1",
                    asset_id=asset.asset_id,
                ),
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                # SYSTEM actor 绕过应用层归属校验，直接触达数据库复合外键：
                # 不存在的资产引用被 FK 拒绝。
                await tickets.create(
                    tenant,
                    CreateTicket(
                        ticket_id=f"ticket-{uuid4().hex}",
                        requester_id="user-1",
                        channel="web",
                        title="Broken asset",
                        actor_type=ActorType.SYSTEM,
                        actor_id="worker",
                        asset_id="no-such-asset",
                    ),
                )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                # 跨租户引用：other 租户的工单不能绑定 tenant 的资产（FK 租户作用域）。
                await tickets.create(
                    other,
                    CreateTicket(
                        ticket_id=f"ticket-{uuid4().hex}",
                        requester_id="user-2",
                        channel="web",
                        title="Cross tenant asset",
                        actor_type=ActorType.SYSTEM,
                        actor_id="worker",
                        asset_id=asset.asset_id,
                    ),
                )
        finally:
            await assets.close()
            await tickets.close()

    asyncio.run(run())


def test_customer_asset_binding_ownership_is_enforced_before_insert(monkeypatch):
    tenant = f"tenant-{uuid4().hex}"

    async def run():
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        await setup_postgres()
        assets = await AssetRepository.connect(DATABASE_URL)
        tickets = await TicketRepository.connect(DATABASE_URL)
        try:
            mine = await assets.create(tenant, create_request(f"asset-{uuid4().hex}", "A-OWN-001"))
            other_asset = await assets.create(tenant, create_request(f"asset-{uuid4().hex}", "A-OWN-002"))
            await assets.update(tenant, other_asset.asset_id, UpdateAsset(owner_user_id="user-2"))
            unassigned = await assets.create(tenant, create_request(f"asset-{uuid4().hex}", "A-OWN-003"))
            await assets.update(tenant, unassigned.asset_id, UpdateAsset(owner_user_id=None))

            # 客户绑定不存在的资产 -> 应用层更早抛 AssetBindingError（而非 FK 错误）。
            with pytest.raises(AssetBindingError):
                await tickets.create(
                    tenant,
                    CreateTicket(
                        ticket_id=f"ticket-{uuid4().hex}",
                        requester_id="user-1",
                        channel="web",
                        title="Missing asset",
                        actor_type=ActorType.CUSTOMER,
                        actor_id="user-1",
                        asset_id="no-such-asset",
                    ),
                )
            # 客户绑定他人资产 -> AssetBindingError。
            with pytest.raises(AssetBindingError):
                await tickets.create(
                    tenant,
                    CreateTicket(
                        ticket_id=f"ticket-{uuid4().hex}",
                        requester_id="user-1",
                        channel="web",
                        title="Others asset",
                        actor_type=ActorType.CUSTOMER,
                        actor_id="user-1",
                        asset_id=other_asset.asset_id,
                    ),
                )
            # 客户绑定未分配使用人的资产 -> AssetBindingError。
            with pytest.raises(AssetBindingError):
                await tickets.create(
                    tenant,
                    CreateTicket(
                        ticket_id=f"ticket-{uuid4().hex}",
                        requester_id="user-1",
                        channel="web",
                        title="Unassigned asset",
                        actor_type=ActorType.CUSTOMER,
                        actor_id="user-1",
                        asset_id=unassigned.asset_id,
                    ),
                )
            # 客户绑定本人资产 -> 成功。
            created = await tickets.create(
                tenant,
                CreateTicket(
                    ticket_id=f"ticket-{uuid4().hex}",
                    requester_id="user-1",
                    channel="web",
                    title="Own asset",
                    actor_type=ActorType.CUSTOMER,
                    actor_id="user-1",
                    asset_id=mine.asset_id,
                ),
            )
            return created
        finally:
            await assets.close()
            await tickets.close()

    created = asyncio.run(run())
    assert created.asset_id == mine.asset_id
