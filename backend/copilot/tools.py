"""Copilot 专用只读工具 —— 统一知识证据契约（{content, evidence}）。

与 src/my_agent/helpdesk/tools.py 保持一致：search_knowledge 返回
JSON {"content": 展示文本, "evidence": [KnowledgeEvidence...]}；
Agent 看到 content（可读），系统经 tool_adapter 保留 evidence 供引用门禁。
全部只读，无副作用工具；租户隔离由 Runtime 仓库层强制。

安全边界：
    - 工具只接收结构化参数；租户/用户统一从 RunContext 获取
    - 返回值经过治理层（tool_adapter.governed_invoke）后才进入 Agent 循环
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from backend.knowledge.identity import retrieval_principal


def _runtime(config: RunnableConfig | None) -> Any:
    return (config or {}).get("configurable", {}).get("runtime")


def _context(config: RunnableConfig | None):
    runtime = _runtime(config)
    if runtime is None or not hasattr(runtime, "context"):
        raise RuntimeError("工具缺少服务端运行上下文")
    return runtime.context


def _knowledge_result(result) -> str:
    """把检索结果组装为统一契约 {content, evidence, retrieval_mode} 的 JSON 字符串。

    retrieval_mode: "lexical-only" | "hybrid"（阶段二：显式标记，进入
    tool_trace / copilot_runs / 指标 / 草稿元数据，评测两组分开）。
    degraded: 向量检索失败降级标记（hybrid 配置但实际 lexical-only）。
    """
    hits = result.hits
    evidence = [
        {
            "document_id": h.document_id,
            "document_version": h.document_version,
            "chunk_id": h.chunk_id,
            "title": h.title,
            "content": h.content[:500],
        }
        for h in hits
    ]
    if not hits:
        content = "知识库未找到相关内容"
    else:
        content = "知识库命中：\n" + "\n".join(
            f"- [{h.document_id}] {h.title}: {h.content[:80]}{'…' if len(h.content) > 80 else ''}"
            for h in hits
        )
    return json.dumps(
        {
            "content": content,
            "evidence": evidence,
            "retrieval_mode": result.retrieval_mode,
            "degraded": getattr(result, "degraded", False),
        },
        ensure_ascii=False,
    )


@tool
async def search_knowledge(
    query: str,
    *,
    limit: int = 5,
    config: RunnableConfig | None = None,
) -> str:
    """搜索当前租户的知识库（统一 KnowledgeRetriever，含部门 ACL），返回结构化证据。

    阶段二：经 runtime.knowledge_retriever.search() 执行，按 embedding 配置
    自动切换 lexical-only / hybrid；retrieval_mode 显式标记进工具输出。
    输出 JSON：{"content", "evidence", "retrieval_mode", "degraded"}。
    引用门禁只接受 evidence 中的三元组。
    """
    if not query or not query.strip() or len(query) > 1_024:
        return json.dumps(
            {
                "content": "错误：查询不能为空且不能超过 1024 字符",
                "evidence": [],
                "retrieval_mode": "lexical-only",
                "degraded": False,
            },
            ensure_ascii=False,
        )
    if limit < 1 or limit > 20:
        return json.dumps(
            {
                "content": "错误：limit 必须在 1 到 20 之间",
                "evidence": [],
                "retrieval_mode": "lexical-only",
                "degraded": False,
            },
            ensure_ascii=False,
        )
    runtime = _runtime(config)
    context = _context(config)
    principal = retrieval_principal(context)
    retriever = getattr(runtime, "knowledge_retriever", None)
    if retriever is None:
        # 降级兼容：无统一检索门面时直接 lexical（测试/旧装配）
        from backend.knowledge.retriever import KnowledgeRetrievalResult

        hits = await runtime.knowledge.lexical_search(principal, query, limit=limit)
        result = KnowledgeRetrievalResult(hits=hits, retrieval_mode="lexical-only")
    else:
        result = await retriever.search(principal=principal, query=query, limit=limit)
    return _knowledge_result(result)


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
    keyword = (query or "").strip().lower()
    list_assets = runtime.assets.list_assets
    try:
        assets = await list_assets(
            context.tenant_id,
            owner_user_id=owner_user_id,
            query_text=keyword or None,
            limit=max(limit, 100),
        )
    except TypeError as exc:
        # 兼容尚未升级的注入仓储桩；生产仓储支持 query_text，
        # 应用层仍保留一次精确过滤作为防御。
        if "query_text" not in str(exc):
            raise
        assets = await list_assets(
            context.tenant_id, owner_user_id=owner_user_id, limit=max(limit, 100)
        )
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
