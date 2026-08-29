"""IT 服务台 Agent 真实工具 —— 从 RunContext 取身份，调用后端既有服务。

对应收敛方案阶段三：
- 工具只接收结构化参数；租户/用户统一从 RunContext 获取
  （config → configurable.runtime.context），**不**自行读取 Authorization/请求头。
- search_assets / search_knowledge：只读查询（治理策略 retryable=True，临时失败可重试）。
- send_message：写入 Outbox（append_outbound_message），携带幂等键；
  治理策略 retryable=False（side_effect=True），发送失败不自动重试。

运行机制（LangGraph 1.x）：工具函数声明 ``config: RunnableConfig`` 参数，
LangGraph 注入 config；``config["configurable"]["runtime"]`` 为 AgentRuntime，
其 ``.context`` 即服务端注入的 RunContext，其余属性为各类仓库/服务。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from backend.knowledge.models import RetrievalPrincipal


def _runtime(config: RunnableConfig | None) -> Any:
    return (config or {}).get("configurable", {}).get("runtime")


def _context(config: RunnableConfig | None):
    """从 config 取 RunContext；缺失时报错（治理层会转成工具错误消息）。"""
    runtime = _runtime(config)
    if runtime is None or not hasattr(runtime, "context"):
        raise RuntimeError("工具缺少服务端运行上下文")
    return runtime.context


@tool
async def search_assets(
    query: str = "",
    *,
    owner_user_id: str | None = None,
    limit: int = 10,
    config: RunnableConfig | None = None,
) -> str:
    """搜索当前租户的 IT 资产。

    query 为可选关键字（匹配 hostname / 名称 / 部门 / 型号）；也可按 owner_user_id
    过滤。结果按租户隔离，跨租户资产不可见。
    """
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
async def search_knowledge(
    query: str,
    *,
    limit: int = 5,
    config: RunnableConfig | None = None,
) -> str:
    """搜索当前租户的知识库（lexical 检索，含部门 ACL）。

    返回命中的文档标题与摘要片段；无命中返回空结果。
    """
    if not query or len(query) > 1_024:
        return "错误：查询不能为空且不能超过 1024 字符"
    if limit < 1 or limit > 20:
        return "错误：limit 必须在 1 到 20 之间"
    runtime = _runtime(config)
    context = _context(config)
    principal = RetrievalPrincipal(
        tenant_id=context.tenant_id, departments=frozenset(), internal=True
    )
    hits = await runtime.knowledge.lexical_search(principal, query, limit=limit)
    if not hits:
        return "知识库未找到相关内容"
    lines = [
        f"- [{h.document_id}] {h.title}: {h.content[:80]}{'…' if len(h.content) > 80 else ''}"
        for h in hits
    ]
    return "知识库命中：\n" + "\n".join(lines)


@tool
async def send_message(
    ticket_id: str,
    content: str,
    *,
    config: RunnableConfig | None = None,
) -> str:
    """向工单渠道发送消息（写入 Outbox，异步投递）。

    每次调用生成唯一消息与幂等键：重试/恢复不会重复插入外发消息；
    投递本身由 outbox_worker 负责，本工具不直接调用外部渠道。
    """
    if not ticket_id or len(ticket_id) > 64:
        return "错误：ticket_id 不能为空且不能超过 64 字符"
    if not content or len(content) > 4_096:
        return "错误：content 不能为空且不能超过 4096 字符"
    runtime = _runtime(config)
    context = _context(config)
    message_id = f"tool-{uuid4().hex}"
    idempotency_key = f"tool-send:{context.tenant_id}:{ticket_id}:{message_id}"
    created = await runtime.ticket_operations.append_outbound_message(
        tenant_id=context.tenant_id,
        ticket_id=ticket_id,
        message_id=message_id,
        actor_type="agent",
        actor_id=context.user_id,
        channel="wecom",
        content=content,
        event_id=f"tool-msg-{message_id}",
        idempotency_key=idempotency_key,
        payload={
            "ticket_id": ticket_id,
            "content": content,
            "channel": "wecom",
            "message_id": message_id,
            "source": "agent_tool",
        },
    )
    return "消息已入队，将由 Outbox 异步投递" if created else "消息写入失败（幂等冲突）"


HELPDESK_TOOLS = [search_assets, search_knowledge, send_message]
