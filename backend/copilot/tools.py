"""Copilot 专用只读工具 —— 返回结构化 JSON 证据供引用门禁使用。

与 src/my_agent/helpdesk/tools.py 的区别：
    - 本模块的 search_knowledge 返回 JSON 数组（document_id/version/chunk_id/
      title/content），模型可读、系统可解析为 ToolEvidence
    - 全部只读，无副作用工具；租户隔离由 Runtime 仓库层强制

安全边界：
    - 工具只接收结构化参数；租户/用户统一从 RunContext 获取
    - 返回值经过治理层（tool_adapter.governed_invoke）后才进入 Agent 循环
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from backend.knowledge.models import RetrievalPrincipal


def _runtime(config: RunnableConfig | None) -> Any:
    return (config or {}).get("configurable", {}).get("runtime")


def _context(config: RunnableConfig | None):
    runtime = _runtime(config)
    if runtime is None or not hasattr(runtime, "context"):
        raise RuntimeError("工具缺少服务端运行上下文")
    return runtime.context


@tool
async def search_knowledge(
    query: str,
    *,
    limit: int = 5,
    config: RunnableConfig | None = None,
) -> str:
    """搜索当前租户的知识库（lexical 检索，含部门 ACL），返回结构化 JSON 证据。

    每命中输出一条：
    {"document_id", "document_version", "chunk_id", "title", "content"}。
    引用门禁只接受出现在这些命中里的 (document_id, version, chunk_id)。
    """
    if not query or len(query) > 1_024:
        return json.dumps(
            [{"error": "查询不能为空且不能超过 1024 字符"}], ensure_ascii=False
        )
    if limit < 1 or limit > 20:
        return json.dumps(
            [{"error": "limit 必须在 1 到 20 之间"}], ensure_ascii=False
        )
    runtime = _runtime(config)
    context = _context(config)
    principal = RetrievalPrincipal(
        tenant_id=context.tenant_id, departments=frozenset(), internal=True
    )
    hits = await runtime.knowledge.lexical_search(principal, query, limit=limit)
    payload = [
        {
            "document_id": h.document_id,
            "document_version": h.document_version,
            "chunk_id": h.chunk_id,
            "title": h.title,
            "content": h.content[:500],
        }
        for h in hits
    ]
    return json.dumps(payload, ensure_ascii=False)


@tool
async def search_assets(
    query: str = "",
    *,
    owner_user_id: str | None = None,
    limit: int = 10,
    config: RunnableConfig | None = None,
) -> str:
    """搜索当前租户的 IT 资产，返回可读文本。"""
    if limit < 1 or limit > 50:
        return "错误：limit 必须在 1 到 50 之间"
    runtime = _runtime(config)
    context = _context(config)
    assets = await runtime.assets.list_assets(
        context.tenant_id, owner_user_id=owner_user_id, limit=max(limit, 100)
    )
    keyword = (query or "").strip().lower()
    if keyword:
        assets = [
            a
            for a in assets
            if keyword
            in " ".join(
                str(v).lower()
                for v in (a.hostname, a.name, a.department, a.asset_type, a.asset_no)
                if v
            )
        ]
    assets = assets[:limit]
    if not assets:
        return "未找到匹配的资产"
    lines = [
        f"- {a.asset_id} {a.hostname or a.name or a.asset_no} "
        f"({a.asset_type}) 状态={a.status.value} 归属={a.owner_user_id or '无'}"
        for a in assets
    ]
    return "资产：\n" + "\n".join(lines)


@tool
async def get_ticket_history(
    requester_id: str,
    *,
    limit: int = 5,
    config: RunnableConfig | None = None,
) -> str:
    """查询某客户的历史工单（只读），返回可读文本。"""
    if not requester_id or len(requester_id) > 128:
        return "错误：requester_id 不能为空且不能超过 128 字符"
    if limit < 1 or limit > 20:
        return "错误：limit 必须在 1 到 20 之间"
    runtime = _runtime(config)
    context = _context(config)
    tickets = await runtime.tickets.list_tickets(
        context.tenant_id,
        requester_id=requester_id,
        statuses=(),
        limit=limit,
    )
    if not tickets:
        return "该客户暂无历史工单"
    lines = [
        f"- #{t.ticket_id} [{t.status.value}] {t.category or '未分类'} | {t.title}"
        f"（{t.resolved_at.isoformat() if t.resolved_at else '未解决'}）"
        for t in tickets
    ]
    return "历史工单：\n" + "\n".join(lines)


@tool
async def get_ticket_messages(
    ticket_id: str,
    *,
    limit: int = 20,
    config: RunnableConfig | None = None,
) -> str:
    """查询工单的消息流（只读），返回可读文本。"""
    if not ticket_id or len(ticket_id) > 64:
        return "错误：ticket_id 不能为空且不能超过 64 字符"
    if limit < 1 or limit > 50:
        return "错误：limit 必须在 1 到 50 之间"
    runtime = _runtime(config)
    context = _context(config)
    overview = await runtime.ticket_operations.get_ticket_overview(
        context.tenant_id, ticket_id
    )
    messages = (overview.get("messages") or [])[-limit:]
    if not messages:
        return "该工单暂无消息"
    lines = [
        f"- [{m['created_at'].isoformat()}] {m['direction']} {m['actor_id']}: {m['content']}"
        for m in messages
    ]
    return "工单消息：\n" + "\n".join(lines)


COPILOT_TOOLS = [search_knowledge, search_assets, get_ticket_history, get_ticket_messages]
