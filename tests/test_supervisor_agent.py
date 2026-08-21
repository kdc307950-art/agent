"""Supervisor 多 Agent 图结构测试（不调 LLM，用 fake model，稳定快速）。"""

from langchain_openai import ChatOpenAI

from src.my_agent.supervisor_agent import build_supervisor_agent


def _fake_model():
    return ChatOpenAI(
        api_key="fake",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=0,
    )


def test_graph_has_expected_nodes():
    agent = build_supervisor_agent(model=_fake_model())
    nodes = set(agent.get_graph().nodes.keys())
    assert {"supervisor", "approval", "weather_agent", "calc_agent"} <= nodes


def test_graph_routing_edges():
    agent = build_supervisor_agent(model=_fake_model())
    edges = {(e.source, e.target) for e in agent.get_graph().edges}

    # supervisor → approval（天气/计算都先过审批）
    assert ("supervisor", "approval") in edges
    # approval → 两个子 Agent
    assert ("approval", "weather_agent") in edges
    assert ("approval", "calc_agent") in edges
    # 子 Agent 完成后回到 supervisor（多轮路由）
    assert ("weather_agent", "supervisor") in edges
    assert ("calc_agent", "supervisor") in edges
