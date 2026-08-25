"""运行上下文 —— 一次 Agent 运行（request）的上下文对象。

RunContext 承载：租户/用户身份、thread_id、开始时间等，
贯穿一次请求的鉴权 → 执行 → 审计 → 计量全链路。
"""

from __future__ import annotations

from dataclasses import dataclass
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

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - monotonic())
