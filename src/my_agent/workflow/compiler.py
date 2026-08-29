"""编译层 —— 把 WorkflowSpec JSON 编译成 LangGraph StateGraph。

核心思路：
    JSON 工作流定义（运营可配） --build_workflow_from_json--> 可执行 LangGraph 图

编译出的图完全复用现有运行时能力：checkpointer（Postgres/SQLite）、store、
interrupt 审批、工具治理等 —— 运行时层不需要任何改动。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph, add_messages
from typing_extensions import TypedDict

from ..supervisor_agent import _build_model  # 复用现有模型构建（含 DEEPSEEK_API_KEY 校验）
from .nodes import NODE_REGISTRY, BuildContext
from .schema import END as _SPEC_END
from .schema import START as _SPEC_START
from .schema import WorkflowSpec

logger = logging.getLogger("langgraph.workflow.compiler")


# ========== 加载与校验 ==========
def load_spec(source: str | Path | dict[str, Any]) -> WorkflowSpec:
    """从 dict / JSON 文本 / 文件路径加载并校验 WorkflowSpec。

    校验失败会抛出 pydantic.ValidationError，包含具体的字段错误。
    """
    if isinstance(source, dict):
        return WorkflowSpec.model_validate(source)
    if isinstance(source, Path) or (
        isinstance(source, str)
        and (source.lstrip().startswith("{") is False)
        and Path(source).exists()
    ):
        path = Path(source)
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return WorkflowSpec.model_validate(raw)
    if isinstance(source, str):
        raw = json.loads(source)
        return WorkflowSpec.model_validate(raw)
    raise TypeError(f"不支持的 spec 来源类型: {type(source)}")


# ========== 状态构建 ==========
def build_state(spec: WorkflowSpec) -> type:
    """根据 spec.state 动态生成 LangGraph 状态 TypedDict。"""
    fields: dict[str, Any] = {}
    for name, field_spec in spec.state.items():
        if field_spec.reducer == "add_messages":
            fields[name] = Annotated[list, add_messages]
        else:
            fields[name] = field_spec.python_type
    if not fields:
        fields["messages"] = Annotated[list, add_messages]
    return TypedDict(f"WorkflowState_{spec.name}", fields, total=False)  # type: ignore[operator]  # 动态创建 TypedDict 是合法运行时用法


# ========== 路由函数 ==========
def make_router(field: str, fallback: str = "finish") -> Callable[[dict[str, Any]], str]:
    """生成条件路由函数：读取 state[field]，缺失时回退到 fallback。"""

    def route(state: dict[str, Any]) -> str:
        value = state.get(field)
        if value is None:
            return fallback
        return str(value)

    return route


def _resolve_endpoint(name: str) -> Any:
    """把 spec 中的 "START"/"END" 字符串替换为 LangGraph 常量。"""
    if name == _SPEC_START:
        return START
    if name == _SPEC_END:
        return END
    return name


def _resolve_route_mapping(mapping: dict[str, str]) -> dict[str, str]:
    return {key: _resolve_endpoint(target) for key, target in mapping.items()}


# ========== 主入口 ==========
def build_workflow_from_json(
    spec: str | Path | dict[str, Any],
    *,
    model: Any | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model_name: str | None = None,
    checkpointer: Any | None = None,
    store: Any | None = None,
    tools: dict[str, Any] | None = None,
    node_registry: dict[str, Callable] | None = None,
    tool_call_wrapper: Callable | None = None,
    rag_service: Any | None = None,
) -> Any:
    """把工作流 JSON 编译成可执行 LangGraph 图。

    参数：
        spec: WorkflowSpec（dict / JSON 文本 / 文件路径）
        model: 可选注入的模型（否则用 api_key/base_url/model_name 构建）
        checkpointer/store: 可选持久化（复用现有 runtime 的注入方式）
        tools: 可选工具注册表 {名称: Tool}（默认取 src.my_agent.tools 全部）
        node_registry: 可选节点注册表覆盖（默认 NODE_REGISTRY）
        tool_call_wrapper: 工具治理钩子（backend 传 ToolGovernance.awrap_tool_call）
        rag_service: 可选知识检索与回答服务，由 rag 节点消费

    返回：
        编译好的 CompiledStateGraph（可 invoke / astream / aget_state）
    """
    workflow = load_spec(spec)

    if model is None:
        model = _build_model(api_key=api_key, base_url=base_url, model_name=model_name)

    ctx = BuildContext(
        model=model,
        tools=tools,
        checkpointer=checkpointer,
        store=store,
        tool_call_wrapper=tool_call_wrapper,
        rag_service=rag_service,
    )
    registry = node_registry or NODE_REGISTRY

    state_type = build_state(workflow)
    graph: Any = StateGraph(state_type)

    # 1. 注册节点
    for node_spec in workflow.nodes:
        factory = registry.get(node_spec.type)
        if factory is None:
            raise ValueError(
                f"节点 {node_spec.id} 引用了未注册的节点类型: {node_spec.type}。"
                f"可用类型: {sorted(registry)}"
            )
        impl = factory(node_spec, ctx)
        graph.add_node(node_spec.id, impl)
        logger.debug("注册节点 %s (%s)", node_spec.id, node_spec.type)

    # 2. 连接边
    for edge_spec in workflow.edges:
        source = _resolve_endpoint(edge_spec.source)
        if edge_spec.type == "plain":
            target = _resolve_endpoint(edge_spec.target or "")
            graph.add_edge(source, target)
            logger.debug("普通边: %s -> %s", edge_spec.source, edge_spec.target)
        else:  # route
            router = make_router(edge_spec.field or "")
            mapping = _resolve_route_mapping(edge_spec.mapping)
            graph.add_conditional_edges(source, router, mapping)
            logger.debug(
                "路由边: %s 按 %s 分流 %s",
                edge_spec.source,
                edge_spec.field,
                list(mapping),
            )

    compiled = graph.compile(checkpointer=checkpointer, store=store)
    logger.info(
        "工作流 %s 编译完成: %d 节点, %d 边",
        workflow.name,
        len(workflow.nodes),
        len(workflow.edges),
    )
    return compiled
