"""单 Agent 图构建模块 —— 本项目最核心的 Agent 入口。

职责：
    把「模型 + 工具」编译成一个可执行的 LangGraph StateGraph（agent ⇄ tools 循环），
    支持外部注入 checkpointer（持久化）、store（长期记忆）和模型配置，
    默认使用 DeepSeek 官方 API。

用法：
    agent = build_agent()                                  # 全默认（读 DEEPSEEK_API_KEY）
    agent = build_agent(model=自定义模型, checkpointer=自定义持久化)   # 依赖注入

架构：
    START → agent(LLM) → 有条件继续: 有 tool_calls → tools → 回 agent；否则 → END
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .state import AgentState
from .tools import tools


load_dotenv()

logger = logging.getLogger("langgraph.agent")
if os.getenv("LANGCHAIN_API_KEY"):
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT", "LangGraph-Agent")
    logger.info("LangSmith tracing enabled")


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否值得重试：超时/网络错误，或 HTTP 408/409/429/5xx。"""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    return isinstance(exc, (TimeoutError, OSError)) or status_code in {
        408,
        409,
        429,
        500,
        502,
        503,
        504,
    }


async def _ainvoke_with_retry(model: Any, messages: Any, max_retries: int):
    """带指数退避重试的模型调用：最多重试 max_retries 次，间隔 2^n 秒（封顶 8s）。"""
    for attempt in range(max_retries + 1):
        try:
            return await model.ainvoke(messages)
        except Exception as exc:
            if attempt >= max_retries or not _is_retryable(exc):
                raise
            await asyncio.sleep(min(2**attempt, 8) + 0.1 * attempt)


def _should_continue(state: AgentState) -> str:
    """路由函数：最后一条消息带 tool_calls 就进 tools 节点，否则结束。"""
    last_message = state["messages"][-1]
    return "tools" if getattr(last_message, "tool_calls", None) else "end"


def build_agent(
    *,
    checkpointer=None,
    store=None,
    model_retry_attempts: int = 2,
    model: Any | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model_name: str | None = None,
    tool_call_wrapper=None,
):
    """Build the graph with injectable model and persistence dependencies."""
    if model is None:
        resolved_api_key = (api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        if not resolved_api_key:
            raise RuntimeError("缺少必需环境变量: DEEPSEEK_API_KEY")
        model = ChatOpenAI(
            api_key=resolved_api_key,
            base_url=base_url or "https://api.deepseek.com",
            model=model_name or "deepseek-chat",
            temperature=0,
        )
    model_with_tools = model.bind_tools(tools)

    async def agent_node(state: AgentState) -> dict[str, list[Any]]:
        response = await _ainvoke_with_retry(
            model_with_tools,
            state["messages"],
            model_retry_attempts,
        )
        return {"messages": [response]}

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_node)
    # awrap_tool_call 是工具治理的钩子接入点：租户白名单、scope 校验、超时、审计都在这里触发。
    # 不传则 None，ToolNode 跳过包装——工具调用会完全绕过治理层，生产路径必须传入。
    workflow.add_node("tools", ToolNode(tools, awrap_tool_call=tool_call_wrapper))
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        _should_continue,
        {"tools": "tools", "end": END},
    )
    workflow.add_edge("tools", "agent")

    # 默认用内存 checkpointer（支持异步 ainvoke/astream）；
    # 持久化由调用方传入（backend 会传 AsyncPostgresSaver）。
    if checkpointer is None:
        checkpointer = MemorySaver()

    return workflow.compile(checkpointer=checkpointer, store=store)
