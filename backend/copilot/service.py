"""Resolution Copilot 服务编排 —— 组装上下文 -> 执行 Agent -> 答案门禁。

职责：
    - CopilotService.prepare_context：读取工单、关联资产、消息流等只读上下文
    - CopilotService.generate：执行 ResolutionCopilot（有界工具循环）
    - CopilotService.apply_gate：独立答案门禁（引用白名单/敏感类别/置信度/租户）

关键设计：
    - 门禁独立于模型自述：模型声称的 confidence/引用不可信，
      引用必须落在工具返回的检索证据里，否则标记 needs_human_review
    - 模型失败/超时/工具异常一律不改变工单状态、不创建客户消息
    - 所有生成默认 auto_reply=False：Agent 2 只产出草稿
"""

from __future__ import annotations

import logging
from typing import Any

from .agent import ResolutionCopilot
from .models import CopilotCitation, CopilotRequest, CopilotResult

logger = logging.getLogger("langgraph.copilot")

# 敏感类别：命中即强制人工复核（与 knowledge.AnswerGatePolicy 对齐）
SENSITIVE_CATEGORIES = frozenset({"finance"})
# 最低置信度：低于此值转人工
MIN_CONFIDENCE = 0.80


class CopilotService:
    """Copilot 编排服务：上下文准备 + 生成 + 门禁。

    不持有 runtime 引用（避免装配阶段循环依赖）：
    所有方法在执行时显式接收 runtime 参数。
    """

    def __init__(self, copilot: ResolutionCopilot) -> None:
        self.copilot = copilot

    async def prepare_context(
        self,
        *,
        runtime,
        tenant_id: str,
        ticket_id: str,
    ) -> CopilotRequest:
        """读取工单只读上下文，组装 CopilotRequest。

        只读数据源：工单记录 + 概览（消息流）；身份/并发校验由 API 层完成。
        """
        tickets = runtime.tickets
        ticket = await tickets.get(tenant_id, ticket_id)
        if ticket is None:
            raise LookupError("工单不存在")
        overview = await runtime.ticket_operations.get_ticket_overview(
            tenant_id, ticket_id
        )
        messages = overview.get("messages") or []
        message_text = "\n".join(
            f"[{m.get('direction')}] {m.get('actor_id')}: {m.get('content')}"
            for m in messages[-12:]
        )
        text = f"{ticket.title}\n{ticket.description}\n历史消息：\n{message_text or '（无）'}"
        return CopilotRequest(
            ticket_id=ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_text=text,
            category=ticket.category,
            asset_id=ticket.asset_id,
            current_status=ticket.status.value,
        )

    async def generate(
        self,
        request: CopilotRequest,
        runtime=None,
        *,
        run_context=None,
    ) -> dict[str, Any]:
        """执行 Copilot Agent，返回原始结果（未门禁，含结构化证据）。"""
        return await self.copilot.run(request, runtime=runtime, run_context=run_context)

    async def run_with_tenant(
        self,
        *,
        runtime,
        tenant_id: str,
        ticket_id: str,
        run_context=None,
    ) -> dict[str, Any]:
        """带租户上下文执行完整流程：准备上下文 -> 生成 -> 按实际工具证据门禁。

        返回 {"request", "raw", "result"}；result 为已过门禁的 CopilotResult。

        引用白名单来自 Agent 实际工具结果（search_knowledge 命中的
        ToolEvidence），不额外做独立 lexical 查询——避免"补充查询命中合法
        文档却被误判无效"的问题；权威校验（租户/发布/有效期/ACL）由
        knowledge.lexical_search 的检索 SQL 在工具执行时完成。
        """
        request = await self.prepare_context(
            runtime=runtime, tenant_id=tenant_id, ticket_id=ticket_id
        )
        raw = await self.generate(request, runtime=runtime, run_context=run_context)

        # 从实际工具证据收集引用白名单（search_knowledge 命中）
        allowed: set[tuple[str, int, str]] = set()
        for item in raw.get("tool_evidence") or []:
            if (
                item.get("document_id")
                and item.get("document_version") is not None
                and item.get("chunk_id")
            ):
                allowed.add(
                    (
                        str(item["document_id"]),
                        int(item["document_version"]),
                        str(item["chunk_id"]),
                    )
                )

        gated = self.apply_gate(raw, request=request, allowed_citations=allowed)
        return {"request": request, "raw": raw, "result": gated}

    def apply_gate(
        self,
        result: dict[str, Any],
        *,
        request: CopilotRequest,
        allowed_citations: set[tuple[str, int, str]] | None = None,
    ) -> CopilotResult:
        """独立答案门禁：对 Agent 输出做最终安全裁定。

        门禁规则（PRD 第八节最低门禁）：
            - 无有效引用 -> needs_human_review=true
            - 敏感类别（finance）-> 禁止自动发送（恒 human review）
            - 置信度 < 0.80 -> 转人工
            - 引用不在工具检索证据白名单内 -> 拒绝该引用
            - 工具异常/模型失败 -> 禁止生成确定性结论
        """
        reasons: list[str] = []
        error_code = result.get("error_code")

        if error_code:
            reasons.append(error_code)
            return CopilotResult(
                draft_answer=None,
                troubleshooting_steps=[],
                citations=[],
                confidence=0.0,
                needs_human_review=True,
                reason_codes=reasons,
                tool_trace=result.get("tool_trace", []),
                error_code=error_code,
            )

        raw_citations = result.get("citations") or []
        citations: list[CopilotCitation] = []
        if isinstance(raw_citations, list):
            for item in raw_citations:
                if not isinstance(item, dict):
                    continue
                key = (
                    str(item.get("document_id") or ""),
                    int(item.get("document_version") or 0),
                    str(item.get("chunk_id") or ""),
                )
                # 引用白名单：只接受工具检索证据中的 (文档, 版本, 分块)
                if allowed_citations is not None and key not in allowed_citations:
                    reasons.append("invalid_citation")
                    continue
                if not key[0]:
                    continue
                citations.append(
                    CopilotCitation(
                        document_id=key[0],
                        document_version=key[1],
                        chunk_id=key[2],
                        title=str(item.get("title") or key[0])[:512],
                    )
                )

        confidence = float(result.get("confidence") or 0.0)
        if not citations:
            reasons.append("missing_citations")
        if request.category and any(
            cat in request.category for cat in SENSITIVE_CATEGORIES
        ):
            reasons.append("sensitive_category")
        if confidence < MIN_CONFIDENCE:
            reasons.append("low_confidence")

        needs_human_review = bool(reasons) or bool(result.get("needs_human_review"))
        return CopilotResult(
            draft_answer=result.get("draft_answer") if isinstance(result.get("draft_answer"), str) else None,
            troubleshooting_steps=list(result.get("troubleshooting_steps") or [])[:20],
            citations=citations[:20],
            confidence=round(confidence, 4),
            needs_human_review=needs_human_review,
            reason_codes=list(dict.fromkeys(reasons)) or ["gate_passed"],
            tool_trace=list(result.get("tool_trace") or [])[:64],
        )
