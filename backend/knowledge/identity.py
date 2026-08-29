"""统一检索主体构造 —— 从服务端 RunContext 派生 RetrievalPrincipal（阶段一）。

职责：
    - retrieval_principal(context)：从 RunContext（租户/部门/内部标记）构造
      RetrievalPrincipal，所有知识检索入口统一走这里
    - 禁止在业务代码中手工写 departments=frozenset(), internal=True：
      部门/内部标记必须来自服务端身份（RunContext），前端/模型请求体不能自由提交

关键设计：
    - 单一事实来源：部门与 internal 只在 RunContext 上声明一次，
      检索/引用校验/门禁全部复用同一主体，避免 ACL 判定分叉
    - internal 语义：True = 服务台内部（客服/坐席）可见 internal 文档；
      False = 客户场景（仅 public + 部门 ACL）
"""

from __future__ import annotations

from ..run_context import RunContext
from .models import RetrievalPrincipal


def retrieval_principal(context: RunContext) -> RetrievalPrincipal:
    """从服务端 RunContext 构造统一检索主体。

    部门与 internal 由认证链路/服务端查询填充到 RunContext，
    前端与模型请求体无法注入（工具从 config 取 RunContext，不读请求参数）。
    """
    return RetrievalPrincipal(
        tenant_id=context.tenant_id,
        departments=context.departments,
        internal=context.internal,
    )
