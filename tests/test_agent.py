import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from src.my_agent.agent import _should_continue, build_agent


class FakeChatModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return AIMessage(content="done")


def test_should_continue_routes_to_tools_or_end():
    tool_call = {
        "name": "calculate",
        "args": {"expression": "2 + 2"},
        "id": "call-1",
        "type": "tool_call",
    }
    assert (
        _should_continue({"messages": [AIMessage(content="", tool_calls=[tool_call])]}) == "tools"
    )
    assert _should_continue({"messages": [AIMessage(content="finished")]}) == "end"


def test_build_agent_compiles_with_injected_model():
    graph = build_agent(
        model=FakeChatModel([AIMessage(content="done")]), checkpointer=MemorySaver()
    )
    assert graph is not None


def test_agent_calls_tool_then_finishes():
    tool_call = {
        "name": "calculate",
        "args": {"expression": "2 + 2"},
        "id": "call-1",
        "type": "tool_call",
    }
    model = FakeChatModel(
        [
            AIMessage(content="", tool_calls=[tool_call]),
            AIMessage(content="计算完成"),
        ]
    )
    graph = build_agent(model=model, checkpointer=MemorySaver())

    async def run():
        return await graph.ainvoke(
            {"messages": [HumanMessage(content="算 2+2")]},
            config={"configurable": {"thread_id": "tool-loop"}},
        )

    result = asyncio.run(run())
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "计算完成"
    assert model.calls == 2


def test_checkpoint_persists_conversation():
    model = FakeChatModel([AIMessage(content="first"), AIMessage(content="second")])
    graph = build_agent(model=model, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "persisted-thread"}}

    async def run():
        first = await graph.ainvoke({"messages": [HumanMessage(content="one")]}, config=config)
        second = await graph.ainvoke({"messages": [HumanMessage(content="two")]}, config=config)
        return first, second

    first, second = asyncio.run(run())
    assert first["messages"][-1].content == "first"
    assert second["messages"][-1].content == "second"
    assert len(second["messages"]) == 4
