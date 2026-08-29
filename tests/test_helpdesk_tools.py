"""IT 服务台真实工具测试 —— 只读检索工具 + 消息发送工具。

覆盖（收敛方案阶段三重点）：
- 跨租户资产/知识/历史工单不可见（身份来自 RunContext，工具不自行读身份）；
- 工具缺少运行上下文时安全失败；
- send_message 写入 Outbox 且每次调用携带唯一幂等键（重试/恢复不重复插入）；
- Resolution Copilot 只读工具集不含任何副作用工具。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.assets.models import AssetRecord, AssetStatus
from backend.knowledge.models import RetrievalHit, RetrievalPrincipal
from backend.run_context import RunContext
from backend.tickets.models import TicketRecord
from src.my_agent.helpdesk import TicketStatus
from src.my_agent.helpdesk.tools import (
    HELPDESK_TOOLS,
    RESOLUTION_COPILOT_TOOLS,
    get_ticket_history,
    get_ticket_messages,
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


class FakeTickets:
    def __init__(self, records: list[TicketRecord]):
        self.records = records

    async def list_tickets(
        self,
        tenant_id: str,
        *,
        requester_id=None,
        statuses=(),
        limit=50,
        **kwargs,
    ):
        items = [t for t in self.records if t.tenant_id == tenant_id]
        if requester_id is not None:
            items = [t for t in items if t.requester_id == requester_id]
        return items[:limit]


class FakeTicketOperations:
    def __init__(self, overview: dict | None = None):
        self.messages: list[dict] = []
        self.overview = overview or {"messages": []}

    async def append_outbound_message(self, **kwargs) -> bool:
        self.messages.append(kwargs)
        return True

    async def get_ticket_overview(self, tenant_id: str, ticket_id: str) -> dict:
        return self.overview


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


def _ticket(
    tenant_id: str,
    ticket_id: str,
    requester_id: str,
    title: str,
    status: str = "closed",
    category: str | None = "it.vpn",
) -> TicketRecord:
    now = datetime.now(UTC)
    return TicketRecord(
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        requester_id=requester_id,
        channel="web",
        external_ticket_id=None,
        title=title,
        description="描述",
        status=TicketStatus(status),
        priority="normal",
        category=category,
        asset_id=None,
        assigned_team_id=None,
        assigned_user_id=None,
        version=3,
        metadata={},
        created_at=now,
        updated_at=now,
        resolved_at=now,
        closed_at=now,
    )


def _make_runtime(
    *,
    tenant_id: str = "tenant-a",
    assets=None,
    knowledge=None,
    ticket_operations=None,
    tickets=None,
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
        tickets=tickets or FakeTickets([]),
    )


def _config(runtime) -> dict:
    return {"configurable": {"runtime": runtime}}


def test_helpdesk_tools_are_exported():
    assert {tool.name for tool in HELPDESK_TOOLS} == {
        "search_assets",
        "search_knowledge",
        "get_ticket_history",
        "get_ticket_messages",
        "send_message",
    }


def test_resolution_copilot_toolset_is_read_only():
    """Resolution Copilot 工具集只含只读工具，绝不暴露 send_message 等副作用工具。"""
    assert {tool.name for tool in RESOLUTION_COPILOT_TOOLS} == {
        "search_assets",
        "search_knowledge",
        "get_ticket_history",
        "get_ticket_messages",
    }
    assert "send_message" not in {tool.name for tool in RESOLUTION_COPILOT_TOOLS}


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
    """跨租户知识不可见；返回统一契约 {content, evidence}。"""
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
    payload = json.loads(result)
    assert "Wi-Fi 配置" in payload["content"]
    assert "VPN 配置" not in payload["content"]  # 跨租户知识不可见
    # evidence 只含当前租户命中，且三元组完整（引用白名单来源）
    assert len(payload["evidence"]) == 1
    ev = payload["evidence"][0]
    assert ev["document_id"] == "wifi-001"
    assert ev["document_version"] == 1
    assert ev["chunk_id"] == "c1"


def test_search_knowledge_returns_structured_evidence_for_citations():
    """真实工具输出必须可被 tool_adapter 解析为引用证据（阶段一验收）。"""
    from backend.copilot.tool_adapter import _parse_evidence

    async def run():
        runtime = _make_runtime(
            tenant_id="tenant-a",
            knowledge=FakeKnowledge([_hit("tenant-a", "vpn-guide", "VPN 配置指南")]),
        )
        return await search_knowledge.coroutine(query="vpn", config=_config(runtime))

    raw = asyncio.run(run())
    evidence = _parse_evidence("search_knowledge", raw)
    assert len(evidence) == 1
    assert evidence[0].citation_key == ("vpn-guide", 1, "c1")


def test_search_knowledge_returns_error_json_with_empty_evidence():
    async def run():
        runtime = _make_runtime()
        return await search_knowledge.coroutine(query="", config=_config(runtime))

    payload = json.loads(asyncio.run(run()))
    assert "错误" in payload["content"]
    assert payload["evidence"] == []


def test_tools_fail_safely_without_runtime_context():
    """缺少运行上下文时工具抛出异常（由工具治理层统一转成错误消息并拒绝执行）。"""
    with pytest.raises(RuntimeError, match="运行上下文"):
        asyncio.run(search_assets.coroutine(query="x", config={"configurable": {}}))


def test_get_ticket_history_is_tenant_and_requester_scoped():
    async def run():
        tickets = FakeTickets(
            [
                _ticket("tenant-a", "t-a-1", "user-1", "VPN 过期"),
                _ticket("tenant-a", "t-a-2", "user-other", "他人工单"),
                _ticket("tenant-b", "t-b-1", "user-1", "跨租户工单"),
            ]
        )
        runtime = _make_runtime(tenant_id="tenant-a", tickets=tickets)
        return await get_ticket_history.coroutine(
            requester_id="user-1", limit=5, config=_config(runtime)
        )

    result = asyncio.run(run())
    assert "t-a-1" in result and "VPN 过期" in result
    assert "t-a-2" not in result  # 同一租户但不同请求人不可见
    assert "t-b-1" not in result  # 跨租户不可见


def test_get_ticket_history_validates_input():
    async def run():
        runtime = _make_runtime()
        bad = await get_ticket_history.coroutine(
            requester_id="", config=_config(runtime)
        )
        return bad

    assert "requester_id" in asyncio.run(run())


def test_get_ticket_messages_is_tenant_scoped_and_limited():
    async def run():
        messages = [
            {
                "message_id": f"m-{i}",
                "direction": "inbound" if i % 2 else "outbound",
                "actor_type": "customer" if i % 2 else "agent",
                "actor_id": "u-1",
                "channel": "web",
                "content": f"消息 {i}",
                "created_at": datetime.now(UTC),
            }
            for i in range(5)
        ]
        runtime = _make_runtime(
            ticket_operations=FakeTicketOperations(overview={"messages": messages})
        )
        return await get_ticket_messages.coroutine(
            ticket_id="t-1", limit=2, config=_config(runtime)
        )

    result = asyncio.run(run())
    # limit=2：只返回最后 2 条
    assert "消息 4" in result and "消息 3" in result
    assert "消息 0" not in result and "消息 1" not in result


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
