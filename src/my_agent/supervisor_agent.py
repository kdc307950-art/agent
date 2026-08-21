from __future__ import annotations

import logging
import os
import sqlite3
from typing import Annotated

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph, add_messages
# 注：langgraph 1.x 已标记 create_react_agent 废弃（v2.0 移除，将迁移到
# langchain.agents.create_agent）。当前项目未装 langchain 包，故继续用此 API。
from langgraph.prebuilt import create_react_agent
from langgraph.types import interrupt
from typing_extensions import TypedDict

from .tools import calculate, get_weather

load_dotenv()

logger = logging.getLogger("langgraph.supervisor")


# ========== 1. 共享状态 ==========
class SupervisorState(TypedDict):
    """Supervisor 多 Agent 的共享状态。

    messages: 所有 Agent 共享的对话历史（add_messages 自动追加合并）
    next:     Supervisor 决定的下一个路由目标（weather / calc / finish）
    """

    messages: Annotated[list[BaseMessage], add_messages]
    next: str


# ========== 2. 模型构建（沿用现有依赖注入风格）==========
def _build_model(api_key=None, base_url=None, model_name=None):
    resolved = (api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
    if not resolved:
        raise RuntimeError("缺少必需环境变量: DEEPSEEK_API_KEY")
    return ChatOpenAI(
        api_key=resolved,
        base_url=base_url or "https://api.deepseek.com",
        model=model_name or "deepseek-chat",
        temperature=0,
    )


# ========== 3. 子 Agent（create_react_agent 封装）==========
def _make_weather_agent(model):
    return create_react_agent(
        model,
        [get_weather],
        name="weather_agent",
        prompt="你是天气查询专家，负责回答用户关于城市天气的问题，需要时调用 get_weather 工具。",
    )


def _make_calc_agent(model):
    return create_react_agent(
        model,
        [calculate],
        name="calc_agent",
        prompt="你是数学计算专家，负责计算数学表达式，需要时调用 calculate 工具。",
    )


# ========== 4. Supervisor 节点：路由判断 ==========
_SUPERVISOR_SYSTEM = (
    "你是主管 Agent（Supervisor），负责把用户问题路由给最合适的子 Agent。\n"
    "只能回复一个词，可选目标：\n"
    "- weather：用户想查询天气\n"
    "- calc：用户想做数学计算\n"
    "- finish：问题已经解决，可以结束对话\n"
    "不要回复其他内容，只回复目标词。"
)


def _make_supervisor_node(model):
    async def supervisor_node(state: SupervisorState) -> dict:
        messages = state["messages"]
        resp = await model.ainvoke(
            [{"role": "system", "content": _SUPERVISOR_SYSTEM}] + list(messages)
        )
        decision = (resp.content or "").strip().lower()
        if "weather" in decision:
            nxt = "weather"
        elif "calc" in decision:
            nxt = "calc"
        else:
            nxt = "finish"
        logger.info("Supervisor 路由到: %s", nxt)
        return {"next": nxt}

    return supervisor_node


def _route(state: SupervisorState) -> str:
    return state["next"]


# ========== 5. Human-in-the-loop 审批节点 ==========
def _approval_node(state: SupervisorState) -> dict:
    """路由到子 Agent 前，用 interrupt() 暂停，等待人工审批。

    interrupt(payload) 会暂停图执行，并把 payload 抛给调用方；
    调用方用 Command(resume={"approved": True/False}) 恢复，
    resume 传入的值就是这里 decision 收到的值。
    批准 → 继续路由到子 Agent；拒绝 → 终止并返回一条消息。
    """
    target = state.get("next", "unknown")
    decision = interrupt({"question": f"是否批准将问题交给 {target} 处理？"})
    if not decision.get("approved", False):
        logger.info("用户拒绝路由到 %s", target)
        return {
            "messages": [AIMessage(content=f"[已拒绝] 用户取消了 {target} 的操作。")],
            "next": "finish",
        }
    logger.info("用户批准路由到 %s", target)
    return {}


# ========== 6. 构建 Supervisor 图 ==========
def build_supervisor_agent(
    *,
    checkpointer=None,
    model=None,
    api_key=None,
    base_url=None,
    model_name=None,
):
    """构建 Supervisor 多 Agent 图（含 Human-in-the-loop）。

    架构：
        START → supervisor → approval → weather_agent / calc_agent → supervisor → ... → END

    Human-in-the-loop：supervisor 决定路由后，approval 节点用 interrupt()
    暂停，等待外部用 Command(resume={"approved": ...}) 批准或拒绝。
    """
    if model is None:
        model = _build_model(api_key, base_url, model_name)

    weather_agent = _make_weather_agent(model)
    calc_agent = _make_calc_agent(model)
    supervisor_node = _make_supervisor_node(model)

    workflow = StateGraph(SupervisorState)
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("approval", _approval_node)
    workflow.add_node("weather_agent", weather_agent)
    workflow.add_node("calc_agent", calc_agent)

    workflow.add_edge(START, "supervisor")

    # supervisor 决定路由：天气/计算 → 先进审批节点；结束 → END
    workflow.add_conditional_edges(
        "supervisor",
        _route,
        {
            "weather": "approval",
            "calc": "approval",
            "finish": END,
        },
    )

    # 审批通过 → 路由到对应子 Agent；拒绝 → END
    workflow.add_conditional_edges(
        "approval",
        _route,
        {
            "weather": "weather_agent",
            "calc": "calc_agent",
            "finish": END,
        },
    )

    # 子 Agent 完成后回到 Supervisor 继续判断，形成多轮路由
    workflow.add_edge("weather_agent", "supervisor")
    workflow.add_edge("calc_agent", "supervisor")

    if checkpointer is None:
        conn = sqlite3.connect("checkpoints.db", check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    return workflow.compile(checkpointer=checkpointer)
