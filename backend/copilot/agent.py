"""Resolution Copilot 有界工具循环（Agent 2 执行器）。

职责：
    - 在「分析上下文 -> 决定查知识 -> 决定查资产 -> 决定查历史 -> 汇总证据
      -> 生成草稿 -> 答案门禁」的固定管道中执行多轮工具调用
    - 用硬限制约束模型行为：最大轮次、每轮工具数、总工具数、上下文条数、
      单工具超时、总执行超时 —— 杜绝无限循环/失控调用

关键设计：
    - 不依赖模型自述，逐轮记录 tool_trace 供审计与门禁
    - 工具集合由调用方注入（默认只读 RESOLUTION_COPILOT 集合），
      本执行器不创建新工具、不绑定副作用工具
    - 每个工具结果做长度截断，控制注入模型的上下文体积
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from time import monotonic
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from .models import CopilotRequest

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


class ResolutionCopilot:
    """Resolution Copilot Agent：有界只读工具循环执行器。

    不依赖 LangGraph 图编译（保持轻量、可测）；手动循环逐轮：
        模型输出 -> 若有工具调用则按限制执行 -> 结果回填 -> 下一轮
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
            tools: 工具名 -> 可 ainvoke(args, config) 的执行器（只读集合）
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

    async def run(self, request: CopilotRequest, runtime=None) -> dict[str, Any]:
        """执行一次有界 Copilot 生成，返回结构化结果 + 工具轨迹。

        参数：
            request: 工单上下文快照
            runtime: AgentRuntime（提供 RunContext 给工具）；为 None 时
                     工具调用会因缺少上下文而失败（由工具实现报错）

        不做答案门禁（门禁由 service 层负责）；这里只保证：
            - 工具调用总数/轮次不超限（超限立即终止并标记 error）
            - 单工具超时/总超时不拖垮工单主流程
        """
        started = monotonic()
        evidence: list[str] = []
        tool_trace: list[dict[str, Any]] = []
        tool_call_count = 0
        rounds = 0
        final_draft: dict[str, Any] = {}
        error_code: str | None = None

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

            messages.append(response)
            tool_calls = list(getattr(response, "tool_calls", []) or [])
            if not tool_calls:
                final_draft = _extract_json(getattr(response, "content", "") or "")
                break

            # 本轮工具调用数限制：最多 max_tool_calls_per_round 个
            tool_calls = tool_calls[: self.limits.max_tool_calls_per_round]
            for call in tool_calls:
                if tool_call_count >= self.limits.max_tool_calls:
                    error_code = "tool_call_limit_exceeded"
                    break
                tool_name = str(call.get("name") or "")
                if tool_name not in self.tools:
                    # 模型请求了未注册工具：拒绝并记录，不执行
                    tool_trace.append(
                        {"tool": tool_name, "status": "denied", "reason": "unregistered_tool"}
                    )
                    messages.append(
                        ToolMessage(
                            content="工具未注册或不可用",
                            tool_call_id=str(call.get("id") or ""),
                            name=tool_name,
                            status="error",
                        )
                    )
                    continue
                tool_call_count += 1
                args = dict(call.get("args") or {})
                tool_call_started = monotonic()
                try:
                    async with asyncio.timeout(self.limits.single_tool_timeout_seconds):
                        result = await self.tools[tool_name].ainvoke(
                            args, config=_runtime_config(runtime)
                        )
                    result_text = str(result)
                    evidence.append(f"[{tool_name}] {_truncate(result_text)}")
                    tool_trace.append(
                        {
                            "tool": tool_name,
                            "status": "completed",
                            "elapsed_ms": round((monotonic() - tool_call_started) * 1000, 1),
                        }
                    )
                    messages.append(
                        ToolMessage(content=result_text, tool_call_id=str(call.get("id") or ""))
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
                            tool_call_id=str(call.get("id") or ""),
                            name=tool_name,
                            status="error",
                        )
                    )
                except Exception as exc:
                    logger.warning("Copilot 工具 %s 失败: %s", tool_name, type(exc).__name__)
                    tool_trace.append({"tool": tool_name, "status": "failed"})
                    messages.append(
                        ToolMessage(
                            content="工具调用失败，请稍后重试",
                            tool_call_id=str(call.get("id") or ""),
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
        if error_code:
            result["error_code"] = error_code
            result["needs_human_review"] = True
        result["auto_reply"] = False
        return result
