"""Copilot 工具治理适配器 —— 所有 Agent 2 工具调用统一走 ToolGovernance。

职责：
    - governed_invoke：构造 ToolNode 兼容的 request，经 ToolGovernance.awrap_tool_call
      执行「profile 校验 → scope 校验 → 租户 allowlist → 输入长度 → 超时/重试 → 审计 → 指标」
    - ToolEvidence：工具返回的结构化证据（引用白名单的唯一来源）
    - 模型伪造 send_message 等未注册工具：治理层直接拒绝并记录 denied

关键设计：
    - 权限控制由治理层执行，不信任模型自述/工具集合绑定：
      即使模型请求了集合外工具，awrap_tool_call 也会按 policy 与
      context.allowed_tools 拒绝
    - request.runtime 必须带 RunContext（来自服务端），绝不来自请求体
    - 返回统一 ToolInvocationResult：ok/content/evidence/trace
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import ToolMessage

logger = logging.getLogger("langgraph.copilot")

# Copilot 允许的只读工具（与 RESOLUTION_COPILOT_TOOLS 对齐；这里是最终裁定）
COPILOT_ALLOWED_TOOLS = frozenset(
    {
        "search_knowledge",
        "search_assets",
        "get_ticket_history",
        "get_ticket_messages",
    }
)

# 明确拒绝的副作用工具（模型即使请求也必须被治理层拦下）
COPILOT_DENIED_TOOLS = frozenset({"send_message"})


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    """一条结构化工具证据（引用白名单的唯一来源）。

    search_knowledge 命中 -> (document_id, document_version, chunk_id)；
    其他工具无文档引用字段（None）。content 供审计与门禁上下文。
    """

    tool_name: str
    document_id: str | None = None
    document_version: int | None = None
    chunk_id: str | None = None
    title: str | None = None
    content: str = ""

    @property
    def citation_key(self) -> tuple[str, int, str] | None:
        if self.document_id and self.document_version is not None and self.chunk_id:
            return (self.document_id, self.document_version, self.chunk_id)
        return None


@dataclass(frozen=True, slots=True)
class ToolInvocationResult:
    """一次治理工具调用的统一结果。

    error_code：结构化错误码（阶段四：不靠错误文本判断状态）：
        - denied_scope / denied_tenant / denied_unregistered / denied_input
        - timeout / failed / no_governance
    与 ToolGovernance 的拒绝原因一一对应，供审计/指标/前端精确分类。
    """

    ok: bool
    content: str
    evidence: list[ToolEvidence] = field(default_factory=list)
    status: str = "completed"  # completed / denied / timeout / failed
    denied_reason: str | None = None
    error_code: str | None = None
    # 检索模式（阶段二）：lexical-only / hybrid + 降级标记
    retrieval_mode: str | None = None
    degraded: bool = False


def _parse_evidence(tool_name: str, content: str) -> list[ToolEvidence]:
    """从工具输出中解析结构化证据（阶段一：统一契约 {content, evidence}）。

    search_knowledge 返回 JSON {"content": 展示文本, "evidence": [...]}；
    evidence 项含 {document_id, document_version, chunk_id, title, content}。
    兼容旧格式（纯数组）；解析失败退化为纯文本证据（无引用键）。
    """
    if tool_name != "search_knowledge":
        return []
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return []
    # 新契约：{"content": ..., "evidence": [...]}
    if isinstance(payload, dict):
        items = payload.get("evidence")
        if not isinstance(items, list):
            return []
    elif isinstance(payload, list):
        items = payload  # 旧格式兼容：纯数组
    else:
        return []
    evidence: list[ToolEvidence] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        evidence.append(
            ToolEvidence(
                tool_name=tool_name,
                document_id=item.get("document_id"),
                document_version=item.get("document_version"),
                chunk_id=item.get("chunk_id"),
                title=item.get("title"),
                content=str(item.get("content") or "")[:2_000],
            )
        )
    return evidence


def _extract_display_content(tool_name: str, raw: str) -> str:
    """从工具输出提取模型可见的展示文本（阶段一）。

    search_knowledge 的新契约为 {"content": 展示文本, "evidence": [...]}：
    Agent 看到 content（可读），不暴露证据 JSON 噪音。
    其他工具或旧格式直接返回原文。
    """
    if tool_name != "search_knowledge":
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict) and isinstance(payload.get("content"), str):
        return payload["content"]
    return raw


async def governed_invoke(
    *,
    tool_name: str,
    args: dict[str, Any],
    tool: Any,
    runtime: Any,
    run_context: Any,
    call_id: str,
    execute: Any,
) -> ToolInvocationResult:
    """经 ToolGovernance 执行一次 Copilot 工具调用。

    参数：
        tool_name: 工具名（模型请求的名字）
        args: 工具参数（模型请求的）
        tool: 真实工具对象（LangChain tool）
        runtime: AgentRuntime（含 tool_governance 与业务仓库）
        run_context: RunContext（服务端身份/租户/scopes/allowed_tools）
        call_id: 本次模型调用 id（审计串联）
        execute: async (args) -> Any 的真实执行器（由调用方绑定 tool.ainvoke）
    返回：
        ToolInvocationResult；任何拒绝/超时/失败都不会抛异常到 Agent 主循环
    """
    governance = getattr(runtime, "tool_governance", None)
    if governance is None:
        return ToolInvocationResult(
            ok=False, content="工具治理未配置", status="failed", denied_reason="no_governance"
        )

    request = SimpleNamespace(
        tool_call={
            "name": tool_name,
            "args": args,
            "id": call_id,
            "type": "tool_call",
        },
        tool=tool,
        runtime=SimpleNamespace(context=run_context),
    )

    async def wrapped_execute(_request):
        return await execute(args)

    try:
        result = await governance.awrap_tool_call(request, wrapped_execute)
    except Exception as exc:
        logger.warning("Copilot 工具 %s 治理调用异常: %s", tool_name, type(exc).__name__)
        return ToolInvocationResult(
            ok=False, content="工具调用失败，请稍后重试", status="failed"
        )

    # 治理层返回 ToolMessage(status=error) 表示拒绝/超时/失败
    if isinstance(result, ToolMessage) and result.status == "error":
        content = str(result.content or "")
        # 结构化错误码（阶段四）：映射 ToolGovernance 的固定文案，
        # 不依赖错误文本做状态判断（文案稳定，映射一次后走 error_code）
        error_code, status = _classify_governance_error(content)
        return ToolInvocationResult(
            ok=False,
            content=content,
            status=status,
            denied_reason=content if status == "denied" else None,
            error_code=error_code,
        )

    text = str(result)
    mode, degraded = _extract_retrieval_mode(tool_name, text)
    return ToolInvocationResult(
        ok=True,
        # Agent 看到展示文本（content），evidence 单独保留给引用门禁
        content=_extract_display_content(tool_name, text),
        evidence=_parse_evidence(tool_name, text),
        retrieval_mode=mode,
        degraded=degraded,
    )


def _extract_retrieval_mode(tool_name: str, raw: str) -> tuple[str | None, bool]:
    """从工具输出解析检索模式与降级标记（阶段二）。

    search_knowledge 返回 {"content", "evidence", "retrieval_mode", "degraded"}；
    其他工具或无标记时返回 (None, False)。
    """
    if tool_name != "search_knowledge":
        return None, False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, False
    if not isinstance(payload, dict):
        return None, False
    mode = payload.get("retrieval_mode")
    if mode not in ("lexical-only", "hybrid"):
        return None, False
    return str(mode), bool(payload.get("degraded"))


def _classify_governance_error(content: str) -> tuple[str, str]:
    """把 ToolGovernance 的固定拒绝/失败文案映射为结构化 error_code。

    映射表与 backend/tool_governance.py 的返回文案一一对应：
        "工具未注册或不可用"    -> denied_unregistered（denied）
        "工具调用权限不足"      -> denied_scope（denied）
        "当前租户未启用该工具"  -> denied_tenant（denied）
        "工具输入超过允许长度"  -> denied_input（denied）
        "工具调用超时"          -> timeout（timeout）
        "工具调用失败"          -> failed（failed）
    未知文案一律视为 failed（不猜测为 denied，避免掩盖真实故障）。
    """
    if "未注册" in content:
        return "denied_unregistered", "denied"
    if "权限不足" in content:
        return "denied_scope", "denied"
    if "未启用" in content:
        return "denied_tenant", "denied"
    if "输入超过允许长度" in content:
        return "denied_input", "denied"
    if "超时" in content:
        return "timeout", "timeout"
    if "失败" in content:
        return "failed", "failed"
    return "failed", "failed"
