"""运行上下文 —— 一次 Agent 运行（request）的上下文对象。

RunContext 承载：租户/用户身份、角色、部门、thread_id、开始时间等，
贯穿一次请求的鉴权 → 执行 → 审计 → 计量全链路。

部门与角色来源（阶段一：权限模型收敛）：
    - role / departments / internal 必须是认证主体或服务端查询结果，
      前端/模型请求体不能自由提交（工具从 config 注入本对象取身份）
    - internal=True 表示服务台内部（客服/坐席）检索，可见 internal 文档；
      客户场景传 False
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic


@dataclass(frozen=True, slots=True)
class RunContext:
    """Server-owned context passed to LangGraph and governed tools.

    This object deliberately contains identity and deadlines only. Prompts,
    bearer tokens, API keys, and tool payloads must never be placed here.
    """

    run_id: str
    request_id: str
    tenant_id: str
    user_id: str
    thread_id: str
    scopes: frozenset[str]
    deadline: float
    allowed_tools: frozenset[str] | None = None
    # 权限模型（阶段一）：角色与部门来自认证主体/服务端，不信任请求体
    role: str | None = None
    departments: frozenset[str] = field(default_factory=frozenset)
    internal: bool = False

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - monotonic())
