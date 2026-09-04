"""LANGGraph VPN Diagnosis Agent — 有界只读工具循环（本 agent 执行器）。

模块归属：backend/vpn。设计要点（对齐 docs/product/vpn-diagnosis-agent-contract.md）：
    - 手动循环（参考 backend/copilot/agent.py 的 ResolutionCopilot）：
      模型输出 -> 若有工具调用则经治理执行 -> 结果回填 -> 下一轮，直到模型不再调工具/超限/超时。
    - 硬限制防失控：最大轮次(默认 3)、总工具调用(默认 8)、单轮工具数(默认 2)、
      单工具超时(默认 3s)、总超时(默认 15s)；超限即终止并标记 error_code，不产生任何命令。
    - 所有工具调用经 backend/copilot/tool_adapter.governed_invoke 走 ToolGovernance
      （scope/租户 allowlist/输入长度/超时/重试/审计/指标），不信任模型自述或工具集合绑定。
    - 模型输出解析并校验为 DiagnosisCommand（extra="forbid" + 拒绝禁止命令）；
      结果含 tool_trace / tool_evidence / must_handoff / DiagnosisEvaluation / 最终 DiagnosisCommand。
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

from backend.copilot.tool_adapter import (
    ToolEvidence,
    ToolInvocationResult,
    governed_invoke,
)

from .models import (
    DEFAULT_HANDOFF_CONFIDENCE,
    DiagnosisCommand,
    DiagnosisRequest,
    evaluate_handoff,
)
from .rules import (
    AccountStatus,
    EvidenceDiagnosis,
    GatewayStatus,
    VpnEvidence,
    evaluate_evidence,
    normalize_account_status,
    normalize_gateway_status,
)
from .runtime_view import RuntimeView

logger = logging.getLogger("langgraph.vpn")

# 有界限制（契约 §6 初始值）
DIAGNOSIS_MAX_ROUNDS = 3
DIAGNOSIS_MAX_TOOL_CALLS = 8
DIAGNOSIS_MAX_TOOL_CALLS_PER_ROUND = 2
DIAGNOSIS_SINGLE_TOOL_TIMEOUT_SECONDS = 3.0
DIAGNOSIS_TOTAL_TIMEOUT_SECONDS = 15.0
MAX_TOOL_RESULT_CHARS = 1_200


@dataclass(frozen=True, slots=True)
class DiagnosisLimits:
    """VPN Diagnosis 执行限制（不可变，测试可注入更小值验证终止）。"""

    max_rounds: int = DIAGNOSIS_MAX_ROUNDS
    max_tool_calls: int = DIAGNOSIS_MAX_TOOL_CALLS
    max_tool_calls_per_round: int = DIAGNOSIS_MAX_TOOL_CALLS_PER_ROUND
    single_tool_timeout_seconds: float = DIAGNOSIS_SINGLE_TOOL_TIMEOUT_SECONDS
    total_timeout_seconds: float = DIAGNOSIS_TOTAL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if (
            self.max_rounds < 1
            or self.max_tool_calls < 1
            or self.max_tool_calls_per_round < 1
            or self.max_tool_calls_per_round > self.max_tool_calls
            or not math.isfinite(self.single_tool_timeout_seconds)
            or self.single_tool_timeout_seconds <= 0
            or not math.isfinite(self.total_timeout_seconds)
            or self.total_timeout_seconds <= 0
        ):
            raise ValueError("VPN Diagnosis 限制参数必须为正数且为有限值")


def _truncate(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断）"


def _system_prompt(request: DiagnosisRequest) -> str:
    """生成本 agent 的系统提示：只允许只读查询，禁止任何副作用动作。"""
    return (
        "你是 VPN 诊断助手：为客服/坐席诊断 VPN 故障，产出结构化处置命令。\n"
        "当前工单：\n"
        f"- 标题/内容：{request.ticket_text}\n"
        f"- 故障分类：{request.fault}\n"
        f"- 状态：{request.current_status}\n"
        f"- 请求人：{request.requester_id}\n"
        f"- 关联资产：{request.asset_id or '无'}\n\n"
        "可用工具（全部只读）：search_vpn_knowledge / get_asset / "
        "get_vpn_account_status / get_vpn_gateway_status / "
        "get_recent_similar_tickets / get_incident_status。\n"
        "硬性规则：\n"
        "1. 只能调用上述只读工具；禁止发送消息、重置密码、解锁账号、授予权限、"
        "修改配置、重启网关、关闭工单等任何副作用动作。\n"
        "2. 基于工具证据诊断；证据不足时不要编造，明确证据不足。\n"
        '3. 输出 JSON：{"command": 允许命令之一(ask_customer/provide_steps/assign_agent/'
        'escalate_incident/request_approval), "content": 命令文本, '
        '"payload": {}, "reason_codes": [...], "confidence": 0.0-1.0}。\n'
        f"4. 置信度低于 {DEFAULT_HANDOFF_CONFIDENCE} 或证据不足时必须转人工。\n"
        "5. command 只能是上述 5 个允许命令，禁止任何其他命令。"
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
    """规范化模型工具调用，保证每个 AI call 有唯一 ID 和字典参数。

    与 ResolutionCopilot 一致：兼容缺失/重复 ID 或字符串参数，规范化后的 AIMessage
    与后续 ToolMessage 使用同一组 ID，避免下一轮请求被上游拒绝。
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
            try:
                response.tool_calls = message_calls
            except Exception:
                pass
    return response, calls


def _parse_command(payload: dict[str, Any]) -> DiagnosisCommand | None:
    """从模型输出解析 DiagnosisCommand；格式非法/禁止命令返回 None（交上层转人工）。"""
    if not isinstance(payload, dict):
        return None
    try:
        return DiagnosisCommand.model_validate(payload)
    except Exception as exc:
        logger.warning("VPN DiagnosisCommand 解析/校验失败: %s", type(exc).__name__)
        return None


def _tool_evidence(tool_name: str, content: str) -> list[ToolEvidence]:
    """从 VPN 工具返回的 JSON 提取证据（found / evidence），供 evaluate_handoff 判定。

    governed_invoke 内的 _parse_evidence 只识别 copilot 的 ``search_knowledge``，
    VPN 工具（search_vpn_knowledge / get_vpn_gateway_status / get_asset 等）返回的
    ``{"content", "found", "evidence"}`` 会被丢弃，导致证据恒为空、恒判 no_evidence
    -> 恒转人工，自动建议命令永远出不来。这里直接从 VPN 工具的展示文本（对非
    ``search_knowledge`` 而言即完整 JSON）解析 found/evidence，使「有依据」能正确
    反映到 no_evidence 判定。
    """
    if not content:
        return []
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    items: list[ToolEvidence] = []
    found = payload.get("found")
    if isinstance(found, bool) and found:
        # found:true 的查询结果本身就是支撑结论的依据
        items.append(ToolEvidence(tool_name=tool_name, content=content[:2_000]))
    raw_evidence = payload.get("evidence")
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if not isinstance(item, dict):
                continue
            items.append(
                ToolEvidence(
                    tool_name=tool_name,
                    document_id=item.get("document_id"),
                    document_version=item.get("document_version"),
                    chunk_id=item.get("chunk_id"),
                    title=item.get("title"),
                    content=str(item.get("content") or "")[:2_000],
                )
            )
    return items


def build_evidence_chain_result(
    request: DiagnosisRequest, evidence: Any, rounds: int
) -> dict[str, Any]:
    """把工具证据与请求上下文归一化为结构化证据链诊断（M3 真实口径）。

    用 rules.evaluate_evidence 产出 hypothesis/confidence/evidence/ruled_out/
    next_action/reason_codes，并返回 JSON 兼容 dict。工具证据为空/不可解析时
    account/gateway 信号为 UNKNOWN，规则按 no_evidence 兜底，不抛异常。
    """
    account = AccountStatus.UNKNOWN
    gateway = GatewayStatus.UNKNOWN
    for item in evidence or []:
        content = getattr(item, "content", None)
        tool_name = getattr(item, "tool_name", "") or (
            item.get("tool_name") if isinstance(item, dict) else ""
        )
        if content is None and isinstance(item, dict):
            content = item.get("content") or item.get("evidence") or ""
        if not content:
            continue
        # 账号状态工具：get_vpn_account_status / get_asset 归属（账号信号）。
        # 网关状态工具：get_vpn_gateway_status（网关信号）。
        if "account" in str(tool_name):
            account = normalize_account_status(_status_from_content(content))
        elif "gateway" in str(tool_name):
            gateway = normalize_gateway_status(_status_from_content(content))
        else:
            # 未知工具：从展示文本粗略推断账号/网关信号。
            text = str(content).casefold()
            if "账号" in text or "状态=active" in text:
                account = normalize_account_status(_status_from_content(content))
            if "网关" in text or "status=" in text:
                gateway = normalize_gateway_status(_status_from_content(content))
    diagnosis: EvidenceDiagnosis = evaluate_evidence(
        VpnEvidence(
            fault=request.fault,
            account_status=account,
            gateway_status=gateway,
            client_version="",
            required_version="",
            error_code="",
            multi_user_impact=(request.fault == "multi_user_impact"),
            identity_ok=request.identity_ok,
            knowledge_hit=bool(evidence),
            has_asset=bool(getattr(request, "asset_id", None)),
        )
    )
    result = diagnosis.to_dict()
    result["rounds"] = rounds
    return result


_STATUS_KW_RE = re.compile(r"(active|locked|disabled|expired|up|degraded|down)", re.IGNORECASE)


def _status_from_content(content: str) -> dict[str, str]:
    """从工具返回的展示文本中提取状态关键词，返回 ``{"status": ...}`` 供信号归一化。"""
    text = str(content or "")
    match = _STATUS_KW_RE.search(text.casefold())
    return {"status": match.group(0).lower() if match else "unknown"}


class VpnDiagnosisAgent:
    """VPN Diagnosis Agent：有界只读工具循环执行器。

    不依赖 LangGraph 图编译；手动循环逐轮：
        模型输出 -> 若有工具调用则经治理执行 -> 结果回填 -> 下一轮
    直到模型不再调用工具、超限或超时。产生结构化 DiagnosisCommand 并做 handoff 判定。
    """

    def __init__(
        self,
        *,
        model,
        tools: dict[str, Any],
        limits: DiagnosisLimits | None = None,
    ) -> None:
        """构造执行器。

        model: 可 ainvoke(messages, config) 的模型（结构化输出/工具调用）
        tools: 工具名 -> LangChain 工具对象（只读集合；执行经治理层）
        limits: 执行限制；默认契约 §6 初始值
        """
        bind = getattr(model, "bind_tools", None)
        if callable(bind):
            model = bind(list(tools.values()))
        self.model = model
        self.tools = tools
        self.limits = limits or DiagnosisLimits()

    async def run(
        self,
        request: DiagnosisRequest,
        runtime=None,
        *,
        run_context=None,
    ) -> dict[str, Any]:
        """执行一次有界 VPN 诊断，返回结构化结果 + 工具轨迹 + 证据 + handoff 判定。

        失败/超限时 error_code 非空且不产生 DiagnosisCommand（上层按 must_handoff 转人工）。
        """
        started = monotonic()
        evidence: list[ToolEvidence] = []
        tool_trace: list[dict[str, Any]] = []
        tool_call_count = 0
        rounds = 0
        final_command: DiagnosisCommand | None = None
        error_code: str | None = None

        messages: list[Any] = [SystemMessage(content=_system_prompt(request))]
        messages.append(
            HumanMessage(content="请分析工单上下文，需要时调用只读工具收集证据，最后输出结构化命令。")
        )

        # 生产 AgentRuntime 没有 .context；工具从 config["configurable"]["runtime"].context
        # 读取 RunContext（租户/身份/scope）。用 RuntimeView 把 RunContext 挂到 .context 上，
        # 其余属性（vpn_adapter/metrics/tool_governance/audit/...）委托代理到底层 runtime，
        # 保证工具能取到 runtime.context.tenant_id 与 runtime.vpn_adapter（Mock 数据源），
        # 与单元测试桩（SimpleNamespace(context=...)）行为一致，且不污染共享 AgentRuntime。
        tool_runtime = RuntimeView(runtime, run_context) if runtime is not None else runtime

        while rounds < self.limits.max_rounds:
            rounds += 1
            remaining_total = self.limits.total_timeout_seconds - (monotonic() - started)
            if remaining_total <= 0:
                error_code = "diagnosis_timeout"
                break
            try:
                async with asyncio.timeout(remaining_total):
                    response = await self.model.ainvoke(messages, config=_runtime_config(tool_runtime))
            except TimeoutError:
                error_code = "diagnosis_timeout"
                break
            except Exception as exc:
                logger.warning("VPN Diagnosis 模型调用失败: %s", type(exc).__name__)
                error_code = "model_failed"
                break

            response, tool_calls = _normalise_tool_calls(response, rounds)
            messages.append(response)
            if not tool_calls:
                final_command = _parse_command(
                    _extract_json(getattr(response, "content", "") or "")
                )
                break

            # 同时约束单轮与总调用数。
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
                        {"tool": tool_name, "status": "denied", "reason": "tool_call_limit_exceeded"}
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
                tool_obj = self.tools[tool_name]
                tool_runner = getattr(tool_obj, "coroutine", None) or tool_obj.ainvoke
                use_coroutine = hasattr(tool_obj, "coroutine")

                async def run_tool(
                    tool_args: dict[str, Any],
                    _runner=tool_runner,
                    _use_coroutine=use_coroutine,
                    _runtime=tool_runtime,
                ) -> Any:
                    cfg = _runtime_config(_runtime)
                    if _use_coroutine:
                        return await _runner(**tool_args, config=cfg)
                    return await _runner(tool_args, config=cfg)

                try:
                    async with asyncio.timeout(
                        self.limits.single_tool_timeout_seconds + 2.0  # 治理包装自身开销余量
                    ):
                        invocation: ToolInvocationResult = await governed_invoke(
                            tool_name=tool_name,
                            args=args,
                            tool=tool_obj,
                            runtime=tool_runtime,
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
                metrics = getattr(tool_runtime, "metrics", None)
                if metrics is not None:
                    metrics.increment(
                        "vpn_tool_calls_total",
                        attributes={"tool": tool_name, "status": invocation.status},
                    )
                if invocation.ok:
                    evidence.extend(invocation.evidence)
                    # VPN 工具证据由 _parse_evidence 丢弃，此处从工具结果 JSON 补回，
                    # 使「有依据」能反映到 no_evidence 判定，避免恒转人工。
                    evidence.extend(_tool_evidence(tool_name, invocation.content))
                    tool_trace.append(
                        {"tool": tool_name, "status": "completed", "elapsed_ms": elapsed_ms}
                    )
                    messages.append(
                        ToolMessage(content=_truncate(invocation.content), tool_call_id=call_id)
                    )
                else:
                    if invocation.error_code in ("denied_scope", "denied_tenant"):
                        if metrics is not None:
                            metrics.increment("vpn_acl_rejected_total")
                    tool_trace.append(
                        {
                            "tool": tool_name,
                            "status": invocation.status,
                            "elapsed_ms": elapsed_ms,
                            "reason": invocation.denied_reason,
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

        if final_command is None and error_code is None:
            error_code = "round_limit_exceeded"

        # handoff 判定（确定性）：无命令/有 error 时强制转人工
        if final_command is None:
            evaluation = evaluate_handoff(
                evidence=evidence,
                fault=request.fault,
                confidence=0.0,
                identity_ok=request.identity_ok,
            )
            result: dict[str, Any] = {
                "command": None,
                "tool_trace": tool_trace,
                "tool_evidence": [
                    {
                        "tool_name": e.tool_name,
                        "document_id": e.document_id,
                        "document_version": e.document_version,
                        "chunk_id": e.chunk_id,
                        "title": e.title,
                        "content": e.content[:2_000],
                    }
                    for e in evidence
                ],
                "must_handoff": True,
                "evaluation": evaluation.model_dump(mode="json"),
            }
        else:
            evaluation = evaluate_handoff(
                evidence=evidence,
                fault=request.fault,
                confidence=final_command.confidence,
                identity_ok=request.identity_ok,
            )
            result = {
                "command": final_command.model_dump(mode="json"),
                "tool_trace": tool_trace,
                "tool_evidence": [
                    {
                        "tool_name": e.tool_name,
                        "document_id": e.document_id,
                        "document_version": e.document_version,
                        "chunk_id": e.chunk_id,
                        "title": e.title,
                        "content": e.content[:2_000],
                    }
                    for e in evidence
                ],
                "must_handoff": evaluation.must_handoff,
                "evaluation": evaluation.model_dump(mode="json"),
            }

        if error_code:
            result["error_code"] = error_code
            result["must_handoff"] = True

        # M5 真实口径：暴露 agent.run 的轮次与工具调用数（供评测核算，而非工具轮数 proxy）。
        result["rounds"] = rounds
        result["tool_call_count"] = tool_call_count

        # 阶段三：暴露结构化证据链假设（hypothesis/confidence/evidence/ruled_out/
        # next_action/reason_codes），给 M3 真实口径与上层人工接管判定消费。
        result["evidence_chain"] = build_evidence_chain_result(request, evidence, rounds)
        return result
