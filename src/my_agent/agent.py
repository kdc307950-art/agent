from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
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
    for attempt in range(max_retries + 1):
        try:
            return await model.ainvoke(messages)
        except Exception as exc:
            if attempt >= max_retries or not _is_retryable(exc):
                raise
            await asyncio.sleep(min(2**attempt, 8) + 0.1 * attempt)


def _should_continue(state: AgentState) -> str:
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
    workflow.add_node("tools", ToolNode(tools, awrap_tool_call=tool_call_wrapper))
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        _should_continue,
        {"tools": "tools", "end": END},
    )
    workflow.add_edge("tools", "agent")

    if checkpointer is None:
        conn = sqlite3.connect("checkpoints.db", check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    return workflow.compile(checkpointer=checkpointer, store=store)
