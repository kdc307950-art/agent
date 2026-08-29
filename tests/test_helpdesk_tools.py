"""IT 服务台真实工具测试 —— search_assets / search_knowledge / send_message。

覆盖（收敛方案阶段三重点）：
- 跨租户资产/知识不可见（身份来自 RunContext，工具不自行读身份）；
- 工具缺少运行上下文时安全失败；
- send_message 写入 Outbox 且每次调用携带唯一幂等键（重试/恢复不重复插入）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.assets.models import AssetRecord, AssetStatus
from backend.knowledge.models import RetrievalHit, RetrievalPrincipal
from backend.run_context import RunContext
from src.my_agent.helpdesk.tools import (
    HELPDESK_TOOLS,
    search_assets,
    search_knowledge,
    send_message,
)


class FakeAssets:
    def __init__(self, records: list[AssetRecord]):
        self.records = records

    async def list_assets(self, tenant_id: str, *, owner_user_id=None, limit=100):
        items = [a for a in self.records if a.tenant_id == tenant_id]
        if owner_user_id is not None:
            items = [a for a in items if a.owner_user_id == owner_user_id]
        return items[:limit]


class FakeKnowledge:
    def __init__(self, hits: list[RetrievalHit]):
        self.hits = hits

    async def lexical_search(self, principal: RetrievalPrincipal, query: str, limit=10):
        return [h for h in self.hits if h.tenant_id == principal.tenant_id][:limit]


class FakeTicketOperations:
    def __init__(self):
        self.messages: list[dict] = []

    async def append_outbound_message(self, **kwargs) -> bool:
        self.messages.append(kwargs)
        return True


def _asset(tenant_id: str, asset_id: str, hostname: str) -> AssetRecord:
    now = datetime.now(UTC)
    return AssetRecord(
        tenant_id=tenant_id,
        asset_id=asset_id,
        asset_no=f"no-{asset_id}",
        asset_type="laptop",
        name=hostname,
        hostname=hostname,
        ip_address=None,
        department="it",
        owner_user_id="user-1",
        uuid=None,
        serial=None,
        status=AssetStatus.IN_USE,
        purchased_at=None,
        warranty_expires_at=None,
        location=None,
        custom_fields={},
        is_deleted=False,
        created_at=now,
        updated_at=now,
    )


def _hit(tenant_id: str, document_id: str, title: str) -> RetrievalHit:
    return RetrievalHit(
        tenant_id=tenant_id,
        document_id=document_id,
        document_version=1,
        chunk_id="c1",
        title=title,
        content=f"{title} 的正文内容",
        source_uri=None,
        source="lexical",
        source_rank=1,
    )


def _make_runtime(
    *, tenant_id: str = "tenant-a", assets=None, knowledge=None, ticket_operations=None
):
    context = RunContext(
        run_id="run-1",
        request_id="req-1",
        tenant_id=tenant_id,
        user_id="user-1",
        thread_id="t-1",
        scopes=frozenset({"ticket:agent"}),
        deadline=asyncio.get_running_loop().time() + 30,
    )
    return SimpleNamespace(
        context=context,
        assets=assets or FakeAssets([]),
        knowledge=knowledge or FakeKnowledge([]),
        ticket_operations=ticket_operations or FakeTicketOperations(),
    )


def _config(runtime) -> dict:
    return {"configurable": {"runtime": runtime}}


def test_helpdesk_tools_are_exported():
    assert {tool.name for tool in HELPDESK_TOOLS} == {
        "search_assets",
        "search_knowledge",
        "send_message",
    }


def test_search_assets_is_tenant_scoped():
    async def run():
        runtime = _make_runtime(
            tenant_id="tenant-b",
            assets=FakeAssets(
                [
                    _asset("tenant-a", "a-1", "laptop-a"),
                    _asset("tenant-b", "b-1", "laptop-b"),
                ]
            ),
        )
        return await search_assets.coroutine(query="laptop", config=_config(runtime))

    result = asyncio.run(run())
    assert "laptop-b" in result
    assert "laptop-a" not in result  # 跨租户资产不可见


def test_search_assets_keyword_filter_and_empty():
    async def run():
        runtime = _make_runtime(
            tenant_id="tenant-a",
            assets=FakeAssets(
                [_asset("tenant-a", "a-1", "laptop-a"), _asset("tenant-a", "a-2", "monitor-1")]
            ),
        )
        hit = await search_assets.coroutine(query="monitor", config=_config(runtime))
        miss = await search_assets.coroutine(query="nope", config=_config(runtime))
        return hit, miss

    hit, miss = asyncio.run(run())
    assert "monitor-1" in hit and "laptop-a" not in hit
    assert "未找到" in miss


def test_search_knowledge_is_tenant_scoped():
    async def run():
        runtime = _make_runtime(
            tenant_id="tenant-b",
            knowledge=FakeKnowledge(
                [
                    _hit("tenant-a", "vpn-001", "VPN 配置"),
                    _hit("tenant-b", "wifi-001", "Wi-Fi 配置"),
                ]
            ),
        )
        return await search_knowledge.coroutine(query="配置", config=_config(runtime))

    result = asyncio.run(run())
    assert "Wi-Fi 配置" in result
    assert "VPN 配置" not in result  # 跨租户知识不可见


def test_tools_fail_safely_without_runtime_context():
    """缺少运行上下文时工具抛出异常（由工具治理层统一转成错误消息并拒绝执行）。"""
    with pytest.raises(RuntimeError, match="运行上下文"):
        asyncio.run(search_assets.coroutine(query="x", config={"configurable": {}}))


def test_send_message_writes_outbox_with_unique_idempotency_key():
    async def run():
        ops = FakeTicketOperations()
        runtime = _make_runtime(ticket_operations=ops)
        first = await send_message.coroutine(
            ticket_id="t-1", content="您好，正在处理", config=_config(runtime)
        )
        second = await send_message.coroutine(
            ticket_id="t-1", content="您好，正在处理", config=_config(runtime)
        )
        return first, second, ops.messages

    first, second, messages = asyncio.run(run())
    assert "入队" in first and "入队" in second
    assert len(messages) == 2
    assert all(m["tenant_id"] == "tenant-a" for m in messages)
    assert all(m["actor_type"] == "agent" for m in messages)
    # 每次调用幂等键唯一（含 message_id），重复调用不会产生相同键。
    keys = [m["idempotency_key"] for m in messages]
    assert len(set(keys)) == 2
    assert all(key.startswith("tool-send:tenant-a:t-1:") for key in keys)
    assert all(m["payload"]["source"] == "agent_tool" for m in messages)


def test_send_message_validates_input():
    async def run():
        runtime = _make_runtime()
        bad_ticket = await send_message.coroutine(
            ticket_id="", content="x", config=_config(runtime)
        )
        bad_content = await send_message.coroutine(
            ticket_id="t-1", content="", config=_config(runtime)
        )
        return bad_ticket, bad_content

    bad_ticket, bad_content = asyncio.run(run())
    assert "ticket_id" in bad_ticket
    assert "content" in bad_content
