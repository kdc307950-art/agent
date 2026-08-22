"""WorkflowSpec —— 工作流 JSON 的定义层（借鉴 FastGPT FlowNodeType，精简到本项目场景）。

一个工作流由 3 部分组成：
- state:  共享状态字段（LangGraph state schema），支持 add_messages 归并
- nodes:  节点列表，每个节点由 NODE_REGISTRY 中注册的工厂实现
- edges:  边列表，plain（普通边）或 route（条件路由，读 state 字段分流）

示例：
{
  "schema_version": 1,
  "name": "helpdesk_supervisor",
  "state": {
    "messages": {"type": "messages", "reducer": "add_messages", "required": true},
    "next": {"type": "str"}
  },
  "nodes": [
    {"id": "supervisor", "type": "supervisor", "config": {...}},
    {"id": "approval", "type": "human_approval", "config": {...}}
  ],
  "edges": [
    {"source": "START", "target": "supervisor"},
    {"source": "supervisor", "type": "route", "field": "next",
     "mapping": {"weather": "approval", "calc": "approval", "finish": "END"}}
  ]
}
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 1

# LangGraph 特殊端点（编译时替换为常量）
START = "START"
END = "END"

# 状态字段允许的类型（映射到 Python 类型）
_STATE_TYPE_MAP = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "dict": dict,
    "list": list,
    "messages": list,
}


class StateFieldSpec(BaseModel):
    """单个状态字段定义。"""

    type: Literal["str", "int", "float", "bool", "dict", "list", "messages"] = "str"
    reducer: Literal["none", "add_messages"] = "none"
    required: bool = False
    description: str = ""

    @property
    def python_type(self) -> type:
        return _STATE_TYPE_MAP[self.type]


class NodeSpec(BaseModel):
    """单个节点定义。config 内容由对应节点工厂解释。"""

    model_config = {"extra": "allow"}  # 画布层可附带 layout/position 等元数据

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    # 画布元数据（不影响编译，仅存储/透传）
    metadata: dict[str, Any] = Field(default_factory=dict)


class EdgeSpec(BaseModel):
    """边定义。

    plain: 普通边（source -> target）。
    route: 条件路由边，读 state[field]，按 mapping 分流到目标节点；
           目标为 "END" 表示结束。
    """

    source: str = Field(min_length=1, max_length=64)
    target: str | None = Field(default=None, min_length=1, max_length=64)
    type: Literal["plain", "route"] = "plain"
    field: str | None = None
    mapping: dict[str, str] = Field(default_factory=dict)


class WorkflowSpec(BaseModel):
    """工作流完整定义。"""

    schema_version: int = SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = ""
    state: dict[str, StateFieldSpec] = Field(default_factory=dict)
    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_workflow(self) -> "WorkflowSpec":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 schema_version={self.schema_version}，当前支持 {SCHEMA_VERSION}"
            )

        # 1. 节点 id 唯一
        node_ids = [n.id for n in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            dupes = {nid for nid in node_ids if node_ids.count(nid) > 1}
            raise ValueError(f"节点 id 重复: {sorted(dupes)}")
        node_set = set(node_ids)

        # 2. 边引用必须存在（允许 START/END）
        special = {START, END}
        for edge in self.edges:
            if edge.source not in node_set and edge.source not in special:
                raise ValueError(f"边引用不存在的节点: source={edge.source}")
            if edge.type == "plain":
                if edge.target is None:
                    raise ValueError(f"普通边缺少 target: {edge.source} -> ?")
                if edge.target not in node_set and edge.target not in special:
                    raise ValueError(f"边引用不存在的节点: target={edge.target}")
                if edge.source == END:
                    raise ValueError("END 节点不能有出边")
                if edge.target == START:
                    raise ValueError("START 节点不能有入边")
            else:  # route
                if not edge.field:
                    raise ValueError(f"路由边缺少 field: {edge.source}")
                if not edge.mapping:
                    raise ValueError(f"路由边缺少 mapping: {edge.source}")
                if edge.source == END:
                    raise ValueError("END 节点不能有出边")
                for key, target in edge.mapping.items():
                    if target not in node_set and target != END:
                        raise ValueError(
                            f"路由边映射到不存在的节点: {edge.source} -> {key}: {target}"
                        )
        return self

    def target_names(self) -> set[str]:
        """所有可能被路由到的目标节点名（画布层可用）。"""
        names: set[str] = set()
        for edge in self.edges:
            if edge.type == "plain" and edge.target:
                names.add(edge.target)
            elif edge.type == "route":
                names.update(edge.mapping.values())
        return names - {END}
