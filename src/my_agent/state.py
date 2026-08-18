#状态定义
from typing import Annotated, Sequence
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

class AgentState(TypedDict):
    """Agent 的状态定义，消息列表自动追加"""
    messages: Annotated[Sequence[BaseMessage], add_messages]