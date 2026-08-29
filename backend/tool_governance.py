"""工具治理 —— Agent 调用工具前的安全管控层。

职责：
    - ToolPolicy: 工具策略（租户白名单、scope、超时、重试次数）
    - ToolGovernance.awrap_tool_call: 包一层所有工具调用，执行
      租户白名单校验 → 调用 → 审计 → 失败重试（临时错误）
    - 通过 BuildContext.tool_call_wrapper 注入到每个 ToolNode，
      保证编排图/子 Agent 的所有工具入口都过治理
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
from langchain_core.messages import ToolMessage

from .audit import AuditRepository, NoopAuditRepository
from .metrics import RuntimeMetrics
from .run_context import RunContext

logger = logging.getLogger("langgraph.tool_governance")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    name: str
    required_scopes: frozenset[str]
    timeout_seconds: float
    max_input_chars: int
    retryable: bool
    side_effect: bool


DEFAULT_TOOL_POLICIES: dict[str, ToolPolicy] = {
    "calculate": ToolPolicy(
        name="calculate",
        required_scopes=frozenset({"chat:write"}),
        timeout_seconds=2.0,
        max_input_chars=512,
        retryable=False,
        side_effect=False,
    ),
    "get_weather": ToolPolicy(
        name="get_weather",
        required_scopes=frozenset({"chat:write"}),
        timeout_seconds=5.0,
        max_input_chars=128,
        retryable=True,
        side_effect=False,
    ),
    # Production helpdesk tools use explicit scopes and conservative limits.
    # Implementations can be added to the ToolNode without changing governance.
    "search_assets": ToolPolicy(
        name="search_assets",
        required_scopes=frozenset({"ticket:agent"}),
        timeout_seconds=3.0,
        max_input_chars=512,
        retryable=True,
        side_effect=False,
    ),
    "search_knowledge": ToolPolicy(
        name="search_knowledge",
        required_scopes=frozenset({"ticket:agent"}),
        timeout_seconds=5.0,
        max_input_chars=1_024,
        retryable=True,
        side_effect=False,
    ),
    # Resolution Copilot 只读工具：历史工单 / 消息流，供 Agent 2 汇总上下文。
    # 租户隔离在工具实现层强制（tenant_id 来自 RunContext，不信任入参）。
    "get_ticket_history": ToolPolicy(
        name="get_ticket_history",
        required_scopes=frozenset({"ticket:agent"}),
        timeout_seconds=3.0,
        max_input_chars=512,
        retryable=True,
        side_effect=False,
    ),
    "get_ticket_messages": ToolPolicy(
        name="get_ticket_messages",
        required_scopes=frozenset({"ticket:agent"}),
        timeout_seconds=3.0,
        max_input_chars=512,
        retryable=True,
        side_effect=False,
    ),
    "send_message": ToolPolicy(
        name="send_message",
        required_scopes=frozenset({"ticket:agent"}),
        timeout_seconds=5.0,
        max_input_chars=4_096,
        retryable=False,
        side_effect=True,
    ),
}


# 工具集合（profile）：Agent 可见的工具白名单。
# - intake_agent：受理阶段只需知识/资产查询
# - resolution_copilot：解决阶段追加历史工单/消息流（全部只读）
# - human_action：需要人工执行的副作用动作（未来受审批工具加入这里）
# 运行期通过 RunContext.allowed_tools 注入，ToolGovernance 在 awrap_tool_call
# 里校验 allowed_tools 子集，杜绝模型绕过 profile 直接调用未授权工具。
INTAKE_AGENT_TOOLS: frozenset[str] = frozenset({"search_knowledge", "search_assets"})
RESOLUTION_COPILOT_TOOLS: frozenset[str] = frozenset(
    {"search_knowledge", "search_assets", "get_ticket_history", "get_ticket_messages"}
)
HUMAN_ACTION_TOOLS: frozenset[str] = frozenset({"send_message"})


def _transient(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, OSError, httpx.TimeoutException)):
        return True
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    status_code = status_code or getattr(response, "status_code", None)
    return status_code in {408, 409, 429, 500, 502, 503, 504}


def _tool_message(request: Any, content: str) -> ToolMessage:
    call = request.tool_call
    return ToolMessage(
        content=content,
        name=str(call.get("name", "unknown")),
        tool_call_id=str(call.get("id", "unknown")),
        status="error",
    )


class ToolGovernance:
    """Central policy, timeout, retry, and audit wrapper for all tools."""

    def __init__(
        self,
        audit: AuditRepository | NoopAuditRepository,
        *,
        policies: Mapping[str, ToolPolicy] | None = None,
        tenant_allowlist: Mapping[str, frozenset[str]] | None = None,
        max_retry_attempts: int = 1,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        if max_retry_attempts < 0:
            raise ValueError("工具重试次数不能为负数")
        self.audit = audit
        self.policies = dict(policies or DEFAULT_TOOL_POLICIES)
        self.tenant_allowlist = None if tenant_allowlist is None else dict(tenant_allowlist)
        self.max_retry_attempts = max_retry_attempts
        self.metrics = metrics or RuntimeMetrics()

    def set_metrics(self, metrics: RuntimeMetrics) -> None:
        self.metrics = metrics

    async def _event(
        self,
        context: RunContext | None,
        event_type: str,
        *,
        tool_name: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if context is None:
            return
        try:
            remaining = context.remaining_seconds()
            if remaining <= 0:
                return
            async with asyncio.timeout(min(1.0, remaining)):
                await self.audit.record_event(
                    context,
                    event_type,
                    tool_name=tool_name,
                    status=status,
                    payload=payload,
                )
        except Exception:
            self.metrics.increment("audit_errors_total")
            logger.exception("工具审计写入失败 run_id=%s tool=%s", context.run_id, tool_name)

    @staticmethod
    def _args_size(args: Any) -> int:
        return len(json.dumps(args or {}, ensure_ascii=False, separators=(",", ":"), default=str))

    @staticmethod
    def _tool_name(value: Any) -> str:
        raw = str(value or "")
        if len(raw) <= 128:
            return raw
        return f"{raw[:32]}...{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    async def awrap_tool_call(self, request: Any, execute: Callable[[Any], Awaitable[Any]]) -> Any:
        call = request.tool_call
        tool_name = self._tool_name(call.get("name", ""))
        context = request.runtime.context
        if not isinstance(context, RunContext):
            self.metrics.increment("tool_call_denied_total")
            return _tool_message(request, "工具调用缺少服务端运行上下文")

        policy = self.policies.get(tool_name)
        if policy is None or request.tool is None:
            self.metrics.increment("tool_call_denied_total")
            await self._event(
                context,
                "tool_call_denied",
                tool_name=tool_name or None,
                status="denied",
                payload={"reason": "unregistered_tool"},
            )
            return _tool_message(request, "工具未注册或不可用")

        missing_scopes = policy.required_scopes.difference(context.scopes)
        tenant_tools = (
            None
            if self.tenant_allowlist is None
            else self.tenant_allowlist.get(context.tenant_id, frozenset())
        )
        if missing_scopes:
            self.metrics.increment("tool_call_denied_total")
            await self._event(
                context,
                "tool_call_denied",
                tool_name=tool_name,
                status="denied",
                payload={"reason": "missing_scope", "scopes": sorted(missing_scopes)},
            )
            return _tool_message(request, "工具调用权限不足")
        if context.allowed_tools is not None and tool_name not in context.allowed_tools:
            tenant_tools = frozenset()
        if tenant_tools is not None and tool_name not in tenant_tools:
            self.metrics.increment("tool_call_denied_total")
            await self._event(
                context,
                "tool_call_denied",
                tool_name=tool_name,
                status="denied",
                payload={"reason": "tenant_tool_policy"},
            )
            return _tool_message(request, "当前租户未启用该工具")

        input_chars = self._args_size(call.get("args", {}))
        if input_chars > policy.max_input_chars:
            self.metrics.increment("tool_call_denied_total")
            await self._event(
                context,
                "tool_call_denied",
                tool_name=tool_name,
                status="denied",
                payload={"reason": "input_too_large", "input_chars": input_chars},
            )
            return _tool_message(request, "工具输入超过允许长度")

        await self._event(
            context,
            "tool_call_started",
            tool_name=tool_name,
            status="running",
            payload={"input_chars": input_chars},
        )
        self.metrics.increment("tool_calls_total")
        attempts = 1 + (
            self.max_retry_attempts if policy.retryable and not policy.side_effect else 0
        )
        started = monotonic()
        for attempt in range(1, attempts + 1):
            remaining = context.remaining_seconds()
            if remaining <= 0:
                self.metrics.increment("tool_call_timeout_total")
                await self._event(
                    context,
                    "tool_call_failed",
                    tool_name=tool_name,
                    status="timeout",
                    payload={"attempt": attempt, "reason": "run_deadline"},
                )
                return _tool_message(request, "工具调用超时")
            timeout = min(policy.timeout_seconds, remaining)
            try:
                async with asyncio.timeout(timeout):
                    result = await execute(request)
                if isinstance(result, ToolMessage) and result.status == "error":
                    self.metrics.increment("tool_call_error_total")
                    await self._event(
                        context,
                        "tool_call_failed",
                        tool_name=tool_name,
                        status="error",
                        payload={"attempt": attempt, "reason": "tool_error"},
                    )
                    return result
                self.metrics.increment("tool_call_completed_total")
                await self._event(
                    context,
                    "tool_call_completed",
                    tool_name=tool_name,
                    status="completed",
                    payload={
                        "attempt": attempt,
                        "elapsed_ms": round((monotonic() - started) * 1000, 1),
                    },
                )
                return result
            except asyncio.CancelledError:
                self.metrics.increment("tool_call_cancelled_total")
                await self._event(
                    context,
                    "tool_call_failed",
                    tool_name=tool_name,
                    status="cancelled",
                    payload={"attempt": attempt},
                )
                raise
            except TimeoutError:
                if attempt < attempts:
                    self.metrics.increment("tool_call_retries_total")
                    await self._event(
                        context,
                        "tool_call_retry",
                        tool_name=tool_name,
                        status="retrying",
                        payload={"attempt": attempt, "error_type": "TimeoutError"},
                    )
                    await asyncio.sleep(min(2 ** (attempt - 1), 4, context.remaining_seconds()))
                    continue
                self.metrics.increment("tool_call_timeout_total")
                await self._event(
                    context,
                    "tool_call_failed",
                    tool_name=tool_name,
                    status="timeout",
                    payload={"attempt": attempt},
                )
                return _tool_message(request, "工具调用超时")
            except Exception as exc:
                if attempt < attempts and _transient(exc):
                    self.metrics.increment("tool_call_retries_total")
                    await self._event(
                        context,
                        "tool_call_retry",
                        tool_name=tool_name,
                        status="retrying",
                        payload={"attempt": attempt, "error_type": type(exc).__name__},
                    )
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                self.metrics.increment("tool_call_error_total")
                await self._event(
                    context,
                    "tool_call_failed",
                    tool_name=tool_name,
                    status="failed",
                    payload={"attempt": attempt, "error_type": type(exc).__name__},
                )
                logger.warning(
                    "工具调用失败 run_id=%s tool=%s error=%s",
                    context.run_id,
                    tool_name,
                    type(exc).__name__,
                )
                return _tool_message(request, "工具调用失败，请稍后重试")
