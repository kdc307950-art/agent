"""Agent 状态定义 —— LangGraph 图内共享的 State schema。

AgentState 是一个 TypedDict，messages 字段用 add_messages reducer 标注，
LangGraph 会自动把每次节点返回的新消息追加合并到历史里（而不是覆盖）。
"""

#状态定义
from typing import Annotated, Sequence
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

class AgentState(TypedDict):
    """Agent 的状态定义，消息列表自动追加"""
    messages: Annotated[Sequence[BaseMessage], add_messages]