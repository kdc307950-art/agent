"""Resolution Copilot 有界工具循环（Agent 2 执行器）。

职责：
    - 在「分析上下文 -> 决定查知识 -> 决定查资产 -> 决定查历史 -> 汇总证据
      -> 生成草稿 -> 答案门禁」的固定管道中执行多轮工具调用
    - 用硬限制约束模型行为：最大轮次、每轮工具数、总工具数、上下文条数、
      单工具超时、总执行超时 —— 杜绝无限循环/失控调用

关键设计：
    - 所有工具调用经 tool_adapter.governed_invoke 走 ToolGovernance：
      profile/scope/租户 allowlist/输入长度/超时/重试/审计/指标统一由治理层执行，
      不信任模型自述或工具集合绑定（伪造 send_message 会被拒绝）
    - 收集结构化 ToolEvidence（search_knowledge 命中的引用键），
      作为答案门禁引用白名单的唯一来源
    - 每个工具结果做长度截断，控制注入模型的上下文体积
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from .models import CopilotRequest
from .tool_adapter import (
    ToolEvidence,
    ToolInvocationResult,
    governed_invoke,
)

logger = logging.getLogger("langgraph.copilot")

# 有界限制（PRD 初始值）
MAX_ROUNDS = 3          # 最大轮次（每轮模型可发起多次工具调用）
MAX_TOOL_CALLS = 6      # 总工具调用上限
MAX_TOOL_CALLS_PER_ROUND = 2
MAX_CONTEXT_ITEMS = 12  # 注入模型的上下文条数上限
SINGLE_TOOL_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 12.0
MAX_TOOL_RESULT_CHARS = 1_200  # 单条工具结果截断长度


@dataclass(frozen=True, slots=True)
class CopilotLimits:
    """Copilot 执行限制（不可变，测试可注入更小值验证终止）。"""

    max_rounds: int = MAX_ROUNDS
    max_tool_calls: int = MAX_TOOL_CALLS
    max_tool_calls_per_round: int = MAX_TOOL_CALLS_PER_ROUND
    max_context_items: int = MAX_CONTEXT_ITEMS
    single_tool_timeout_seconds: float = SINGLE_TOOL_TIMEOUT_SECONDS
    total_timeout_seconds: float = TOTAL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if (
            self.max_rounds < 1
            or self.max_tool_calls < 1
            or self.max_tool_calls_per_round < 1
            or self.max_tool_calls_per_round > self.max_tool_calls
            or self.max_context_items < 1
            or not math.isfinite(self.single_tool_timeout_seconds)
            or self.single_tool_timeout_seconds <= 0
            or not math.isfinite(self.total_timeout_seconds)
            or self.total_timeout_seconds <= 0
        ):
            raise ValueError("Copilot 限制参数必须为正数且为有限值")


def _truncate(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断）"


def _system_prompt(request: CopilotRequest) -> str:
    """生成 Copilot 的系统提示：只允许只读查询，禁止任何副作用动作。"""
    return (
        "你是客服解决方案 Copilot：为客服坐席生成带引用的处理建议与回复草稿。\n"
        "当前工单：\n"
        f"- 标题/内容：{request.ticket_text}\n"
        f"- 分类：{request.category or '未分类'}\n"
        f"- 状态：{request.current_status}\n"
        f"- 请求人：{request.requester_id}\n"
        f"- 关联资产：{request.asset_id or '无'}\n\n"
        "可用工具（全部只读）：search_knowledge / search_assets / "
        "get_ticket_history / get_ticket_messages。\n"
        "硬性规则：\n"
        "1. 只能调用上述只读工具；禁止发送消息、修改工单/资产/策略/知识。\n"
        "2. 基于检索证据作答；无证据时明确说明证据不足，不要编造。\n"
        "3. 输出 JSON：{\"draft_answer\": 回复草稿, \"steps\": [排查步骤], "
        "\"citations\": [{\"document_id\": ..., \"document_version\": ..., "
        "\"chunk_id\": ...}], \"confidence\": 0.0-1.0, "
        "\"needs_human_review\": true/false}。\n"
        "4. draft_answer 是给客服看的草稿，不是直接发给客户的消息。"
    )


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出中提取第一个 JSON 对象（容忍 markdown 围栏与前后杂音）。"""
    if not text:
        return {}
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _runtime_config(runtime) -> dict[str, Any]:
    return {"configurable": {"runtime": runtime}}


def _normalise_tool_calls(response: Any, round_number: int) -> tuple[Any, list[dict[str, Any]]]:
    """规范化模型工具调用，保证每个 AI call 都有唯一 ID 和字典参数。

    兼容部分 OpenAI 代理返回的缺失/重复 ID 或字符串参数；规范化后的
    ``AIMessage`` 会与后续 ``ToolMessage`` 使用同一组 ID，避免下一轮请求因
    tool-call 配对不完整而被上游拒绝。
    """
    raw_calls = list(getattr(response, "tool_calls", []) or [])
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_calls):
        call = dict(raw) if isinstance(raw, Mapping) else {}
        supplied_id = str(call.get("id") or "")
        call_id = supplied_id if supplied_id and supplied_id not in seen else ""
        if not call_id:
            call_id = f"call-r{round_number}-{index}"
            while call_id in seen:
                call_id += "-x"
        seen.add(call_id)
        call["id"] = call_id
        call["type"] = str(call.get("type") or "tool_call")
        call["name"] = str(call.get("name") or "")
        if not isinstance(call.get("args"), dict):
            call["args"] = {}
            call["_malformed_args"] = True
        calls.append(call)

    if calls and calls != raw_calls:
        message_calls = [
            {key: value for key, value in call.items() if key != "_malformed_args"}
            for call in calls
        ]
        copier = getattr(response, "model_copy", None)
        if callable(copier):
            response = copier(update={"tool_calls": message_calls})
        else:
            # 测试桩/第三方消息对象可能不是 Pydantic；尽力就地同步 ID。
            try:
                response.tool_calls = message_calls
            except Exception:
                pass
    return response, calls


class ResolutionCopilot:
    """Resolution Copilot Agent：有界只读工具循环执行器。

    不依赖 LangGraph 图编译（保持轻量、可测）；手动循环逐轮：
        模型输出 -> 若有工具调用则经治理执行 -> 结果回填 -> 下一轮
    直到模型不再调用工具、超限或超时。
    """

    def __init__(
        self,
        *,
        model,
        tools: dict[str, Any],
        limits: CopilotLimits | None = None,
    ) -> None:
        """构造执行器。

        参数：
            model: 可 ainvoke(messages, config) 的模型（结构化输出/工具调用）
            tools: 工具名 -> LangChain 工具对象（只读集合；执行经治理层）
            limits: 执行限制；默认 PRD 初始值

        模型绑定：若模型支持 bind_tools（LangChain 系），绑定只读工具集，
        使模型可发起工具调用；测试桩直接返回带 tool_calls 的 AIMessage 时
        无需绑定。运行时（runtime）在执行时由 run() 显式传入，
        避免装配阶段循环依赖。
        """
        bind = getattr(model, "bind_tools", None)
        if callable(bind):
            model = bind(list(tools.values()))
        self.model = model
        self.tools = tools
        self.limits = limits or CopilotLimits()

    async def run(
        self,
        request: CopilotRequest,
        runtime=None,
        *,
        run_context=None,
    ) -> dict[str, Any]:
        """执行一次有界 Copilot 生成，返回结构化结果 + 工具轨迹 + 结构化证据。

        参数：
            request: 工单上下文快照
            runtime: AgentRuntime（提供 tool_governance 与业务仓库）
            run_context: RunContext（服务端身份/租户/scopes/allowed_tools）；
                         工具治理与工具实现都依赖它；为 None 时工具调用会被拒绝

        不做答案门禁（门禁由 service 层负责）；这里只保证：
            - 工具调用总数/轮次不超限（超限立即终止并标记 error）
            - 单工具超时/总超时不拖垮工单主流程
            - 所有调用经 ToolGovernance（权限/租户/审计/指标）
        """
        started = monotonic()
        evidence: list[ToolEvidence] = []
        tool_trace: list[dict[str, Any]] = []
        tool_call_count = 0
        rounds = 0
        final_draft: dict[str, Any] = {}
        error_code: str | None = None
        # 检索模式汇总（阶段二）：最后一次 search_knowledge 的模式与降级标记
        retrieval_modes: list[str] = []
        degraded_flags: list[bool] = []

        messages: list[Any] = [SystemMessage(content=_system_prompt(request))]
        messages.append(
            HumanMessage(
                content="请分析工单上下文，需要时调用只读工具收集证据，最后输出结构化结果。"
            )
        )

        while rounds < self.limits.max_rounds:
            rounds += 1
            remaining_total = self.limits.total_timeout_seconds - (monotonic() - started)
            if remaining_total <= 0:
                error_code = "copilot_timeout"
                break
            try:
                async with asyncio.timeout(remaining_total):
                    response = await self.model.ainvoke(
                        messages, config=_runtime_config(runtime)
                    )
            except TimeoutError:
                error_code = "copilot_timeout"
                break
            except Exception as exc:
                logger.warning("Copilot 模型调用失败: %s", type(exc).__name__)
                error_code = "model_failed"
                break

            response, tool_calls = _normalise_tool_calls(response, rounds)
            messages.append(response)
            if not tool_calls:
                final_draft = _extract_json(getattr(response, "content", "") or "")
                break

            # 同时约束单轮与总调用数。模型消息中声明的每个 tool_call 都必须有
            # 对应 ToolMessage；否则下一轮 OpenAI 兼容接口会拒绝整段消息历史。
            remaining_capacity = max(0, self.limits.max_tool_calls - tool_call_count)
            allowed_count = min(
                len(tool_calls),
                self.limits.max_tool_calls_per_round,
                remaining_capacity,
            )
            allowed_calls = tool_calls[:allowed_count]
            omitted_calls = tool_calls[allowed_count:]
            if omitted_calls:
                error_code = "tool_call_limit_exceeded"
                for _offset, call in enumerate(omitted_calls, start=allowed_count):
                    tool_name = str(call.get("name") or "")
                    call_id = str(call["id"])
                    tool_trace.append(
                        {
                            "tool": tool_name,
                            "status": "denied",
                            "reason": "tool_call_limit_exceeded",
                        }
                    )
                    messages.append(
                        ToolMessage(
                            content="工具调用超过本轮或总量上限，未执行",
                            tool_call_id=call_id,
                            name=tool_name,
                            status="error",
                        )
                    )

            for call in allowed_calls:
                tool_name = str(call.get("name") or "")
                call_id = str(call["id"])
                tool_call_count += 1
                if call.get("_malformed_args"):
                    tool_trace.append(
                        {"tool": tool_name, "status": "denied", "reason": "malformed_tool_call"}
                    )
                    messages.append(
                        ToolMessage(
                            content="工具参数格式无效，未执行",
                            tool_call_id=call_id,
                            name=tool_name,
                            status="error",
                        )
                    )
                    continue
                if tool_name not in self.tools:
                    # 模型请求了未注册工具：拒绝并记录，不执行
                    tool_trace.append(
                        {"tool": tool_name, "status": "denied", "reason": "unregistered_tool"}
                    )
                    messages.append(
                        ToolMessage(
                            content="工具未注册或不可用",
                            tool_call_id=call_id,
                            name=tool_name,
                            status="error",
                        )
                    )
                    continue
                args = dict(call.get("args") or {})
                tool_call_started = monotonic()

                # 经治理层执行：profile/scope/租户/超时/重试/审计/指标
                tool_obj = self.tools[tool_name]
                # 直接调 coroutine 并把 runtime 注入 config：LangChain ainvoke
                # 会把 config 当 Run 配置剥离，不会传给工具声明的 config 参数；
                # coroutine 原样透传。测试桩无 coroutine 时退化为 ainvoke。
                tool_runner = getattr(tool_obj, "coroutine", None) or tool_obj.ainvoke
                use_coroutine = hasattr(tool_obj, "coroutine")

                async def run_tool(
                    tool_args: dict[str, Any],
                    _runner=tool_runner,
                    _use_coroutine=use_coroutine,
                    _runtime=runtime,
                ) -> Any:
                    cfg = _runtime_config(_runtime)
                    if _use_coroutine:
                        return await _runner(**tool_args, config=cfg)
                    return await _runner(tool_args, config=cfg)

                try:
                    async with asyncio.timeout(
                        self.limits.single_tool_timeout_seconds
                        + 2.0  # 治理包装自身开销余量
                    ):
                        invocation: ToolInvocationResult = await governed_invoke(
                            tool_name=tool_name,
                            args=args,
                            tool=tool_obj,
                            runtime=runtime,
                            run_context=run_context,
                            call_id=call_id,
                            execute=run_tool,
                        )
                except TimeoutError:
                    tool_trace.append(
                        {
                            "tool": tool_name,
                            "status": "timeout",
                            "elapsed_ms": round((monotonic() - tool_call_started) * 1000, 1),
                        }
                    )
                    messages.append(
                        ToolMessage(
                            content="工具调用超时",
                            tool_call_id=call_id,
                            name=tool_name,
                            status="error",
                        )
                    )
                    continue

                elapsed_ms = round((monotonic() - tool_call_started) * 1000, 1)
                # 工具调用指标（阶段十：copilot_tool_calls_total{tool,status}）
                metrics = getattr(runtime, "metrics", None)
                if metrics is not None:
                    metrics.increment(
                        "copilot_tool_calls_total",
                        attributes={"tool": tool_name, "status": invocation.status},
                    )
                if invocation.ok:
                    evidence.extend(invocation.evidence)
                    if len(evidence) > self.limits.max_context_items:
                        # 证据白名单与注入模型的上下文都必须有硬上限，避免
                        # 大结果把后续模型请求撑爆；保留最先返回的高排名命中。
                        del evidence[self.limits.max_context_items :]
                    if invocation.retrieval_mode:
                        retrieval_modes.append(invocation.retrieval_mode)
                        degraded_flags.append(invocation.degraded)
                    tool_trace.append(
                        {
                            "tool": tool_name,
                            "status": "completed",
                            "elapsed_ms": elapsed_ms,
                            "retrieval_mode": invocation.retrieval_mode,
                        }
                    )
                    messages.append(
                        ToolMessage(
                            content=_truncate(invocation.content), tool_call_id=call_id
                        )
                    )
                else:
                    # ACL 拒绝指标：scope/租户权限不足（治理层 denied）
                    if invocation.error_code in ("denied_scope", "denied_tenant"):
                        if metrics is not None:
                            metrics.increment("copilot_acl_rejected_total")
                    tool_trace.append(
                        {
                            "tool": tool_name,
                            "status": invocation.status,
                            "elapsed_ms": elapsed_ms,
                            "reason": invocation.denied_reason,
                            # 结构化错误码（阶段四）：不靠错误文本判断状态
                            "error_code": invocation.error_code,
                        }
                    )
                    messages.append(
                        ToolMessage(
                            content=_truncate(invocation.content),
                            tool_call_id=call_id,
                            name=tool_name,
                            status="error",
                        )
                    )
            if error_code:
                break

        if not final_draft and error_code is None:
            # 达到轮次上限仍未产出结构化结果
            error_code = "round_limit_exceeded"

        result = dict(final_draft) if isinstance(final_draft, dict) else {}
        result.setdefault("draft_answer", None)
        result.setdefault("troubleshooting_steps", [])
        result.setdefault("citations", [])
        result.setdefault("confidence", 0.0)
        result.setdefault("needs_human_review", True)
        result.setdefault("reason_codes", [])
        result["tool_trace"] = tool_trace
        # 结构化证据：答案门禁引用白名单的唯一来源
        result["tool_evidence"] = [
            {
                "tool_name": e.tool_name,
                "document_id": e.document_id,
                "document_version": e.document_version,
                "chunk_id": e.chunk_id,
                "title": e.title,
                "content": e.content[:2_000],
            }
            for e in evidence
        ]
        if error_code:
            result["error_code"] = error_code
            result["needs_human_review"] = True
        # 检索模式（阶段二）：取最后一次 search_knowledge 的模式；任一次降级即标记
        result["retrieval_mode"] = retrieval_modes[-1] if retrieval_modes else None
        result["degraded"] = any(degraded_flags) if degraded_flags else False
        result["auto_reply"] = False
        return result
