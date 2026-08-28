"""Workflow 编排包 —— JSON 工作流 → LangGraph 图 编译层。

借鉴 FastGPT 的 FlowNodeType 编排模型，精简到本项目场景：
- schema.WorkflowSpec: 工作流 JSON 定义（state/nodes/edges）
- nodes.NODE_REGISTRY: 节点工厂注册表（supervisor/agent/tool/condition/human_approval/rag）
- compiler.build_workflow_from_json: 编译入口（JSON → CompiledStateGraph）

用法：
    from my_agent.workflow import build_workflow_from_json
    graph = build_workflow_from_json("workflows/legacy-demo.json")
    await graph.ainvoke({"messages": [HumanMessage(content="北京天气？")]}, config=...)
"""

from .compiler import build_workflow_from_json, load_spec
from .nodes import NODE_REGISTRY, BuildContext, register_node_factory
from .schema import EdgeSpec, NodeSpec, StateFieldSpec, WorkflowSpec

__all__ = [
    "WorkflowSpec",
    "NodeSpec",
    "EdgeSpec",
    "StateFieldSpec",
    "BuildContext",
    "NODE_REGISTRY",
    "register_node_factory",
    "load_spec",
    "build_workflow_from_json",
]
