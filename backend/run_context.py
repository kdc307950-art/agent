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
