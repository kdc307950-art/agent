"""内置节点工厂 —— NODE_REGISTRY 的实现在这里。

每个工厂签名：factory(node: NodeSpec, ctx: BuildContext) -> Callable
返回一个 LangGraph 节点（可调用），可以是：
- 普通函数/协程（接收 state dict，返回 partial update）
- 已编译的子图（如 create_agent 的结果，add_node 直接嵌套）

已实现节点类型：
- supervisor        LLM 路由节点（写 route_field，如 next）
- agent             create_agent 子 Agent（绑定指定工具）
- tool              纯工具节点（ToolNode，直接执行一批工具）
- condition         静态规则路由（不调 LLM，读 state 字段按规则表分流）
- human_approval    Human-in-the-loop 审批节点（interrupt 暂停）
- rag               知识库检索节点（占位，第三期接入 pgvector）
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from ..tools import tools as _default_tools
from .schema import NodeSpec

logger = logging.getLogger("langgraph.workflow.nodes")


class BuildContext:
    """节点工厂共享的构建上下文（模型、工具注册表、持久化、工具治理）。"""

    def __init__(
        self,
        *,
        model: Any,
        tools: dict[str, Any] | None = None,
        checkpointer: Any = None,
        store: Any = None,
        tool_call_wrapper: Callable | None = None,
    ):
        self.model = model
        self.tools: dict[str, Any] = dict(tools or _default_tool_map())
        self.checkpointer = checkpointer
        self.store = store
        # 工具治理钩子（租户白名单 / scope / 超时 / 工具审计）。
        # 必须透传到每个执行工具的节点，否则编排图会绕过治理直接调工具。
        self.tool_call_wrapper = tool_call_wrapper

    def build_tool_node(self, names: list[str], **kwargs: Any) -> Any:
        """统一构造受治理的 ToolNode，保证所有工具入口都挂上治理钩子。"""
        from langgraph.prebuilt import ToolNode

        return ToolNode(
            _require_tools(self, names),
            awrap_tool_call=self.tool_call_wrapper,
            **kwargs,
        )


def _default_tool_map() -> dict[str, Any]:
    return {tool.name: tool for tool in _default_tools}


def _require_tools(ctx: BuildContext, names: list[str]) -> list[Any]:
    missing = [name for name in names if name not in ctx.tools]
    if missing:
        raise ValueError(f"工作流引用了未注册的工具: {missing}")
    return [ctx.tools[name] for name in names]


def _build_supervisor_prompt(config: dict[str, Any]) -> str:
    """根据 targets 配置生成路由提示词。"""
    targets = config.get("targets") or {}
    if not targets:
        raise ValueError("supervisor 节点必须配置 targets: {目标词: 描述}")
    lines = ["你是主管 Agent（Supervisor），负责把用户问题路由给最合适的子 Agent。",
             "只能回复一个词，可选目标："]
    for word, desc in targets.items():
        lines.append(f"- {word}：{desc}")
    lines.append("不要回复其他内容，只回复目标词。")
    return config.get("system_prompt") or "\n".join(lines)


# ========== supervisor：LLM 路由 ==========
def supervisor_factory(node: NodeSpec, ctx: BuildContext) -> Callable:
    config = node.config
    targets: dict[str, str] = dict(config.get("targets") or {})
    if not targets:
        raise ValueError(f"节点 {node.id} (supervisor) 缺少 targets 配置")
    route_field = config.get("route_field", "next")
    fallback = config.get("fallback", "finish")
    if fallback not in targets:
        raise ValueError(f"节点 {node.id} (supervisor) 的 fallback={fallback} 不在 targets 中")
    system_prompt = _build_supervisor_prompt(config)
    model = ctx.model

    async def supervisor_node(state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages") or []
        resp = await model.ainvoke(
            [{"role": "system", "content": system_prompt}] + list(messages)
        )
        decision = (getattr(resp, "content", "") or "").strip().lower()
        nxt = fallback
        for word in targets:
            if word in decision:
                nxt = word
                break
        logger.info("Supervisor 路由到: %s", nxt)
        return {route_field: nxt}

    return supervisor_node


# ========== agent：create_agent 子 Agent ==========
def agent_factory(node: NodeSpec, ctx: BuildContext) -> Callable:
    from langchain.agents import create_agent  # 延迟导入，避免启动开销

    config = node.config
    agent_name = config.get("name") or node.id
    prompt = config.get("prompt") or (
        f"你是 {agent_name}，负责处理分配给你的任务，需要时调用可用工具。"
    )
    tool_names = list(config.get("tools") or [])
    if not tool_names:
        raise ValueError(f"节点 {node.id} (agent) 必须配置 tools 列表")
    selected_tools = _require_tools(ctx, tool_names)
    middleware = ()
    if ctx.tool_call_wrapper is not None:
        from langchain.agents.middleware import AgentMiddleware

        class GovernanceMiddleware(AgentMiddleware):
            async def awrap_tool_call(self, request, handler):
                return await ctx.tool_call_wrapper(request, handler)

        middleware = (GovernanceMiddleware(),)
    return create_agent(
        ctx.model,
        selected_tools,
        name=agent_name,
        system_prompt=prompt,
        middleware=middleware,
    )


# ========== tool：纯工具节点 ==========
def tool_factory(node: NodeSpec, ctx: BuildContext) -> Callable:
    config = node.config
    tool_names = list(config.get("tools") or [])
    if not tool_names:
        raise ValueError(f"节点 {node.id} (tool) 必须配置 tools 列表")
    return ctx.build_tool_node(tool_names)


# ========== condition：静态规则路由 ==========
def condition_factory(node: NodeSpec, ctx: BuildContext) -> Callable:
    config = node.config
    field = config.get("field")
    if not field:
        raise ValueError(f"节点 {node.id} (condition) 必须配置 field")
    rules: dict[str, str] = dict(config.get("rules") or {})
    if not rules:
        raise ValueError(f"节点 {node.id} (condition) 必须配置 rules: {值: 目标节点}")
    route_field = config.get("route_field", "next")
    default_target = config.get("default", "finish")

    def condition_node(state: dict[str, Any]) -> dict[str, Any]:
        value = state.get(field)
        # 支持字符串化的值（如 "true"/"1"）归一化
        key = str(value).strip().lower() if value is not None else ""
        if key in rules:
            target = rules[key]
        elif "default" in rules:  # rules 内可带 "default" 兜底键
            target = rules["default"]
        else:
            target = default_target  # config 顶层 default
        logger.info("Condition %s: %s=%r -> %s", node.id, field, value, target)
        return {route_field: target}

    return condition_node


# ========== human_approval：HITL 审批 ==========
def human_approval_factory(node: NodeSpec, ctx: BuildContext) -> Callable:
    config = node.config
    route_field = config.get("route_field", "next")
    question_template = config.get("question_template", "是否批准将问题交给 {next} 处理？")
    reject_message_template = config.get(
        "reject_message", "[已拒绝] 用户取消了 {next} 的操作。"
    )
    reject_next = config.get("reject_next", "finish")

    def approval_node(state: dict[str, Any]) -> dict[str, Any]:
        target = str(state.get(route_field, "unknown"))
        question = question_template.format(next=target)
        decision = interrupt({"question": question})
        approved = bool((decision or {}).get("approved", False))
        if not approved:
            logger.info("用户拒绝路由到 %s", target)
            return {
                "messages": [AIMessage(content=reject_message_template.format(next=target))],
                route_field: reject_next,
            }
        logger.info("用户批准路由到 %s", target)
        return {}

    return approval_node


# ========== rag：知识库检索（占位） ==========
def rag_factory(node: NodeSpec, ctx: BuildContext) -> Callable:
    raise NotImplementedError(
        f"节点 {node.id} (rag) 尚未实现：知识库检索将在第三期接入 pgvector。"
        "可用 supervisor/agent/tool/condition/human_approval 先完成流程编排。"
    )


# ========== 注册表 ==========
NODE_REGISTRY: dict[str, Callable[[NodeSpec, BuildContext], Callable]] = {
    "supervisor": supervisor_factory,
    "agent": agent_factory,
    "tool": tool_factory,
    "condition": condition_factory,
    "human_approval": human_approval_factory,
    "rag": rag_factory,
}


def register_node_factory(node_type: str, factory: Callable) -> None:
    """扩展注册表：注册自定义节点类型（画布层/业务侧可注入新节点）。"""
    if not node_type or not callable(factory):
        raise ValueError("node_type 必须非空且 factory 必须可调用")
    NODE_REGISTRY[node_type] = factory
