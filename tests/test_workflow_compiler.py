"""Workflow 编译层测试（不调 LLM，用 fake model，稳定快速）。

覆盖：spec 加载/校验、JSON→图编译、HITL interrupt 审批流、
condition 节点路由、rag 占位、节点注册表扩展。
"""

import sqlite3

import pytest
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from src.my_agent.workflow import (
    NODE_REGISTRY,
    BuildContext,
    build_workflow_from_json,
    load_spec,
    register_node_factory,
)
from src.my_agent.workflow.nodes import condition_factory, rag_factory
from src.my_agent.workflow.schema import NodeSpec

WORKFLOW_PATH = "workflows/helpdesk_supervisor.json"


def _fake_model():
    return ChatOpenAI(
        api_key="fake",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=0,
    )


def _memory_checkpointer():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return SqliteSaver(conn)


# ========== spec 加载与校验 ==========

def test_load_spec_from_dict():
    spec = load_spec({
        "schema_version": 1,
        "name": "demo",
        "nodes": [{"id": "a", "type": "condition", "config": {"field": "x", "rules": {}}}],
        "edges": [{"source": "START", "target": "a"}],
    })
    assert spec.name == "demo"
    assert len(spec.nodes) == 1


def test_load_spec_from_file():
    spec = load_spec(WORKFLOW_PATH)
    assert spec.name == "helpdesk_supervisor"
    node_types = {n.type for n in spec.nodes}
    assert {"supervisor", "human_approval", "agent"} <= node_types


def test_load_spec_from_json_text():
    spec = load_spec('{"schema_version": 1, "name": "t", "nodes": [], "edges": []}')
    assert spec.name == "t"


def test_invalid_duplicate_node_id():
    with pytest.raises(Exception, match="节点 id 重复"):
        load_spec({
            "schema_version": 1,
            "name": "dup",
            "nodes": [
                {"id": "a", "type": "condition"},
                {"id": "a", "type": "condition"},
            ],
            "edges": [],
        })


def test_invalid_dangling_edge():
    with pytest.raises(Exception, match="不存在的节点"):
        load_spec({
            "schema_version": 1,
            "name": "dangling",
            "nodes": [{"id": "a", "type": "condition"}],
            "edges": [{"source": "a", "target": "ghost"}],
        })


def test_invalid_route_to_missing_node():
    with pytest.raises(Exception, match="不存在的节点"):
        load_spec({
            "schema_version": 1,
            "name": "badroute",
            "nodes": [{"id": "a", "type": "condition"}],
            "edges": [
                {"source": "START", "target": "a"},
                {"source": "a", "type": "route", "field": "next",
                 "mapping": {"x": "ghost"}},
            ],
        })


def test_invalid_route_missing_mapping():
    with pytest.raises(Exception, match="mapping"):
        load_spec({
            "schema_version": 1,
            "name": "noroute",
            "nodes": [{"id": "a", "type": "condition"}],
            "edges": [
                {"source": "START", "target": "a"},
                {"source": "a", "type": "route", "field": "next"},
            ],
        })


# ========== 编译结构 ==========

def test_compile_helpdesk_structure():
    agent = build_workflow_from_json(WORKFLOW_PATH, model=_fake_model())
    nodes = set(agent.get_graph().nodes.keys())
    assert {"supervisor", "approval", "weather_agent", "calc_agent"} <= nodes

    edges = {(e.source, e.target) for e in agent.get_graph().edges}
    assert ("supervisor", "approval") in edges          # 路由展开
    assert ("approval", "weather_agent") in edges
    assert ("approval", "calc_agent") in edges
    assert ("weather_agent", "supervisor") in edges     # 子 Agent 回环
    assert ("calc_agent", "supervisor") in edges


def test_compile_unknown_node_type():
    with pytest.raises(ValueError, match="未注册的节点类型"):
        build_workflow_from_json({
            "schema_version": 1,
            "name": "unknown",
            "nodes": [{"id": "x", "type": "not_a_type"}],
            "edges": [{"source": "START", "target": "x"}],
        }, model=_fake_model())


def test_rag_node_placeholder():
    with pytest.raises(NotImplementedError, match="pgvector"):
        rag_factory(NodeSpec(id="r", type="rag", config={}), BuildContext(model=_fake_model()))


# ========== condition 节点 ==========

def test_condition_node_routing():
    node = condition_factory(
        NodeSpec(
            id="c",
            type="condition",
            config={"field": "ticket_type", "rules": {"refund": "refund_agent", "default": "end"}},
        ),
        BuildContext(model=_fake_model()),
    )
    assert node({"ticket_type": "refund"}) == {"next": "refund_agent"}
    assert node({"ticket_type": "unknown"}) == {"next": "end"}
    assert node({}) == {"next": "end"}


# ========== HITL 审批流（内存 SQLite checkpointer） ==========

_APPROVAL_MIN = {
    "schema_version": 1,
    "name": "approval_min",
    "state": {
        "messages": {"type": "messages", "reducer": "add_messages"},
        "next": {"type": "str"},
    },
    "nodes": [
        {"id": "approval", "type": "human_approval",
         "config": {"question_template": "是否批准将问题交给 {next} 处理？",
                    "reject_message": "[已拒绝] 用户取消了 {next} 的操作。"}},
    ],
    "edges": [
        {"source": "START", "target": "approval"},
        {"source": "approval", "type": "route", "field": "next",
         "mapping": {"weather": "END", "finish": "END"}},
    ],
}


def test_approval_reject_flow():
    agent = build_workflow_from_json(
        _APPROVAL_MIN, model=_fake_model(), checkpointer=_memory_checkpointer()
    )
    config = {"configurable": {"thread_id": "reject_t"}}

    agent.invoke({"messages": [HumanMessage(content="退款")], "next": "weather"}, config)
    snap = agent.get_state(config)
    assert snap.tasks, "应当暂停在 interrupt 上"
    question = snap.tasks[0].interrupts[0].value["question"]
    assert question == "是否批准将问题交给 weather 处理？"

    agent.invoke(Command(resume={"approved": False}), config)
    snap2 = agent.get_state(config)
    assert not snap2.tasks, "拒绝后不应再有挂起审批"
    assert snap2.values["next"] == "finish"
    last = snap2.values["messages"][-1]
    assert "[已拒绝]" in last.content


def test_approval_approve_flow():
    agent = build_workflow_from_json(
        _APPROVAL_MIN, model=_fake_model(), checkpointer=_memory_checkpointer()
    )
    config = {"configurable": {"thread_id": "approve_t"}}

    agent.invoke({"messages": [HumanMessage(content="天气")], "next": "weather"}, config)
    snap = agent.get_state(config)
    assert snap.tasks, "应当暂停在 interrupt 上"

    agent.invoke(Command(resume={"approved": True}), config)
    snap2 = agent.get_state(config)
    assert not snap2.tasks, "批准后流程应走完"
    assert snap2.values["next"] == "weather"  # 批准保留原路由目标
    msgs = snap2.values["messages"]
    assert all("[已拒绝]" not in m.content for m in msgs)


# ========== 注册表扩展 ==========

def test_register_custom_node():
    def custom_factory(node, ctx):
        def custom_node(state):
            return {"next": "finish"}
        return custom_node

    register_node_factory("custom_test", custom_factory)
    try:
        assert "custom_test" in NODE_REGISTRY
        agent = build_workflow_from_json({
            "schema_version": 1,
            "name": "custom",
            "nodes": [{"id": "c", "type": "custom_test"}],
            "edges": [
                {"source": "START", "target": "c"},
                {"source": "c", "type": "route", "field": "next", "mapping": {"finish": "END"}},
            ],
        }, model=_fake_model())
        assert "c" in agent.get_graph().nodes
    finally:
        NODE_REGISTRY.pop("custom_test", None)


# ========== 运行时回环 E2E（自定义节点，不依赖 LLM/网络） ==========

def test_loop_workflow_runs_to_end():
    """验证 JSON 编译出的图能真实执行：环 + 条件结束 + 状态累计。"""

    def loop_factory(node, ctx):
        limit = node.config.get("limit", 2)

        def loop_node(state):
            count = state.get("count", 0) + 1
            nxt = "loop_node" if count < limit else "finish"
            return {"count": count, "next": nxt}

        return loop_node

    register_node_factory("loop_test", loop_factory)
    try:
        agent = build_workflow_from_json({
            "schema_version": 1,
            "name": "loop_demo",
            "state": {
                "count": {"type": "int"},
                "next": {"type": "str"},
            },
            "nodes": [{"id": "loop_node", "type": "loop_test", "config": {"limit": 3}}],
            "edges": [
                {"source": "START", "target": "loop_node"},
                {"source": "loop_node", "type": "route", "field": "next",
                 "mapping": {"loop_node": "loop_node", "finish": "END"}},
            ],
        }, model=_fake_model(), checkpointer=_memory_checkpointer())

        result = agent.invoke({"count": 0, "next": "loop_node"},
                              {"configurable": {"thread_id": "loop_t"}})
        assert result["count"] == 3, "环应执行 3 次后结束"
        assert result["next"] == "finish"
    finally:
        NODE_REGISTRY.pop("loop_test", None)
