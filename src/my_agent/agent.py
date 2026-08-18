
import logging
import sqlite3
import os
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





def build_agent():
    """构建带有 SQLite 检查点持久化的 LangGraph Agent"""

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
    def agent_node(state: AgentState) -> dict:
        response = model_with_tools.invoke(state["messages"])
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

    # 🆕 正确创建 SQLite 检查点（直接传入连接对象）
    conn = sqlite3.connect("checkpoints.db", check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    # 编译并返回
    return workflow.compile(checkpointer=checkpointer)
