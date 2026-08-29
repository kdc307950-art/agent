"""LangChain/LangGraph 最小兼容性测试（收敛方案阶段五）。

依赖版本策略：以 uv.lock 已验证版本为基线，pyproject 设兼容上限（<2.0）。
每次升级核心包（langchain / langgraph / langchain-openai / checkpoint-*）必须
执行 `uv lock --upgrade-package <pkg>` + 全量 pytest，且本文件四个契约全部通过：
- Agent 能绑定工具；
- ToolNode 能执行工具；
- interrupt/resume 能恢复；
- PostgreSQL checkpointer 能初始化（依赖数据库时）。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from src.my_agent.tools import calculate

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


def test_tool_node_executes_core_tool():
    """ToolNode 能执行已注册工具（calculate，经编译图以生产等价方式运行）。"""
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode
    from typing_extensions import TypedDict

    class State(TypedDict, total=False):
        messages: list

    tool_node = ToolNode([calculate])
    graph = (
        StateGraph(State)
        .add_node("tools", tool_node)
        .add_edge(START, "tools")
        .add_edge("tools", END)
        .compile()
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "calculate",
                "args": {"expression": "2+2"},
                "id": "call-compat-1",
                "type": "tool_call",
            }
        ],
    )
    result = graph.invoke({"messages": [message]})
    tool_message = result["messages"][-1]
    assert tool_message.content == "计算结果: 4"
    assert tool_message.tool_call_id == "call-compat-1"


def test_agent_binds_tools():
    """Agent 能绑定工具（fake chat model，不调用外部模型）。"""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    model = FakeMessagesListChatModel(responses=[])
    agent = create_agent(model, [calculate])
    assert agent is not None
    assert agent.name


def test_interrupt_resume_recovers_with_memory_saver():
    """interrupt/resume 在 MemorySaver 下能恢复（HITL 基础契约）。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
    from typing_extensions import TypedDict

    class State(TypedDict, total=False):
        result: str

    def approval_node(state: State) -> dict:
        decision = interrupt({"question": "是否批准？"})
        return {"result": f"approved={decision}"}

    graph = (
        StateGraph(State)
        .add_node("approval", approval_node)
        .add_edge(START, "approval")
        .add_edge("approval", END)
        .compile(checkpointer=MemorySaver())
    )
    config = {"configurable": {"thread_id": "compat-interrupt-1"}}

    first = graph.invoke({}, config)
    assert "__interrupt__" in first  # 图停在审批点

    resumed = graph.invoke(Command(resume={"approved": True}), config)
    assert resumed["result"] == "approved={'approved': True}"


@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
def test_postgres_checkpointer_initializes():
    """PostgreSQL checkpointer 能连接并初始化（生产持久化前提）。"""

    async def run() -> bool:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
            return checkpointer is not None

    assert asyncio.run(run()) is True
