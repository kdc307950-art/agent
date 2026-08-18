
import asyncio
import logging
import os
import sqlite3
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.sqlite import SqliteSaver
from .state import AgentState
from .tools import tools
load_dotenv()
# ====== 🆕 P5: LangSmith 可观测性配置 ======
# 如果 .env 中配置了 LANGCHAIN_API_KEY，自动启用追踪
if os.getenv("LANGCHAIN_API_KEY"):
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT", "LangGraph-Agent")
    logging.getLogger("langgraph.agent").info("LangSmith tracing enabled")

load_dotenv()

# ... 后续代码保持不变 ...





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


async def _ainvoke_with_retry(model, messages, max_retries: int):
    for attempt in range(max_retries + 1):
        try:
            return await model.ainvoke(messages)
        except Exception as exc:
            if attempt >= max_retries or not _is_retryable(exc):
                raise
            await asyncio.sleep(min(2**attempt, 8) + 0.1 * attempt)


def build_agent(*, checkpointer=None, store=None, model_retry_attempts: int = 2):
    """Build the graph with a production checkpointer or local SQLite fallback."""

    # 模型初始化（DeepSeek）
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少必需环境变量: DEEPSEEK_API_KEY")
    model = ChatOpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=0,
    )
    model_with_tools = model.bind_tools(tools)

    # 节点
    async def agent_node(state: AgentState) -> dict:
        response = await _ainvoke_with_retry(
            model_with_tools,
            state["messages"],
            model_retry_attempts,
        )
        return {"messages": [response]}

    tool_node = ToolNode(tools)

    # 路由
    def should_continue(state: AgentState) -> str:
        last_msg = state["messages"][-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "tools"
        return "end"

    # 构建图
    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "end": END}
    )
    workflow.add_edge("tools", "agent")

    if checkpointer is None:
        conn = sqlite3.connect("checkpoints.db", check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    return workflow.compile(checkpointer=checkpointer, store=store)
