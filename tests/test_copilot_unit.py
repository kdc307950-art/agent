"""Resolution Copilot 单元测试 —— Agent 限制 / 答案门禁 / 安全用例。

覆盖 PRD 第十节单元测试清单（前 6 条 + 门禁相关）：
1. Agent 2 只能看到只读工具（工具集合不含副作用工具）
2. 工具调用超过上限会终止
3. 单工具超时不会拖垮工单
4. 不同租户历史工单不可见（工具层隔离，见 test_helpdesk_tools）
5. 未发布知识不能成为引用（白名单来源过滤）
6. 无引用时不能自动回复（恒 needs_human_review）
7. finance 类别始终人工复核
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from backend.copilot.agent import CopilotLimits, ResolutionCopilot, _extract_json
from backend.copilot.models import CopilotRequest
from backend.copilot.service import CopilotService
from backend.run_context import RunContext


def _request(**overrides) -> CopilotRequest:
    base = {
        "ticket_id": "t-1",
        "requester_id": "user-1",
        "ticket_text": "VPN 无法连接，需要排查",
        "category": "it.vpn",
        "asset_id": None,
        "current_status": "assigned",
    }
    base.update(overrides)
    return CopilotRequest(**base)


class _Tool:
    """可 ainvoke 的桩工具：记录调用次数，可按脚本返回结果。"""

    def __init__(self, name: str, results: list[Any] | None = None):
        self.name = name
        self.calls: list[dict] = []
        self.results = list(results or ["ok"])
        self.timeout_on: int | None = None

    async def ainvoke(self, args: dict, config=None):
        self.calls.append(args)
        if self.timeout_on is not None and len(self.calls) >= self.timeout_on:
            await asyncio.sleep(10)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


class _ToolCallModel:
    """桩模型：第一轮发起工具调用，第二轮输出 JSON。"""

    def __init__(self, tool_names: list[str], final: dict | None = None):
        self.tool_names = tool_names
        self.final = final or {
            "draft_answer": "请检查网络连接",
            "troubleshooting_steps": ["检查网络"],
            "citations": [],
            "confidence": 0.95,
            "needs_human_review": False,
        }
        self.round = 0

    async def ainvoke(self, messages, config=None):
        from langchain_core.messages import AIMessage

        self.round += 1
        if self.round == 1:
            return AIMessage(
                content="需要查知识",
                tool_calls=[
                    {
                        "name": name,
                        "args": {"query": "vpn 排查"},
                        "id": f"call-{i}",
                        "type": "tool_call",
                    }
                    for i, name in enumerate(self.tool_names)
                ],
            )
        import json

        return AIMessage(content=json.dumps(self.final, ensure_ascii=False))


class _AlwaysToolCallModel:
    """每轮都发起工具调用的模型：验证上限终止。"""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self.round = 0

    async def ainvoke(self, messages, config=None):
        from langchain_core.messages import AIMessage

        self.round += 1
        return AIMessage(
            content="继续",
            tool_calls=[
                {
                    "name": self.tool_name,
                    "args": {"query": "x"},
                    "id": f"call-{self.round}",
                    "type": "tool_call",
                }
            ],
        )


def _runtime():
    context = RunContext(
        run_id="run-copilot",
        request_id="req-1",
        tenant_id="tenant-a",
        user_id="user-1",
        thread_id="t-1",
        scopes=frozenset({"ticket:agent"}),
        deadline=asyncio.get_running_loop().time() + 60,
    )
    return SimpleNamespace(context=context)


def _copilot(tools: dict[str, Any], model, limits: CopilotLimits | None = None):
    return ResolutionCopilot(model=model, tools=tools, limits=limits)


def test_extract_json_tolerates_noise():
    assert _extract_json('前文 {"a": 1} 后文') == {"a": 1}
    assert _extract_json("```json\n{\"b\": 2}\n```") == {"b": 2}
    assert _extract_json("no json") == {}


def test_copilot_runs_tool_loop_and_produces_structured_result():
    async def run():
        tool = _Tool("search_knowledge")
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["search_knowledge"]),
        )
        return await copilot.run(_request()), tool.calls

    result, calls = asyncio.run(run())
    assert result["draft_answer"] == "请检查网络连接"
    assert result["confidence"] == 0.95
    assert result["auto_reply"] is False
    assert len(calls) == 1
    assert result["tool_trace"][0]["tool"] == "search_knowledge"
    assert result["tool_trace"][0]["status"] == "completed"


def test_tool_call_limit_terminates_run():
    """工具调用超过上限（max_tool_calls）立即终止并标记错误。"""
    async def run():
        tool = _Tool("search_knowledge")
        copilot = _copilot(
            {"search_knowledge": tool},
            _AlwaysToolCallModel("search_knowledge"),
            limits=CopilotLimits(
                max_rounds=10, max_tool_calls=2, max_tool_calls_per_round=2
            ),
        )
        return await copilot.run(_request()), tool.calls

    result, calls = asyncio.run(run())
    assert len(calls) <= 2
    assert result["error_code"] == "tool_call_limit_exceeded"
    assert result["needs_human_review"] is True


def test_round_limit_terminates_run():
    """达到最大轮次仍未产出结构化结果时标记 round_limit_exceeded。"""
    async def run():
        tool = _Tool("search_knowledge")
        copilot = _copilot(
            {"search_knowledge": tool},
            _AlwaysToolCallModel("search_knowledge"),
            limits=CopilotLimits(
                max_rounds=1, max_tool_calls=10, max_tool_calls_per_round=1
            ),
        )
        return await copilot.run(_request())

    result = asyncio.run(run())
    assert result["error_code"] == "round_limit_exceeded"
    assert result["needs_human_review"] is True


def test_single_tool_timeout_does_not_break_run():
    """单工具超时被捕获：工具调用标记 timeout，主流程继续/正常收尾。"""
    async def run():
        tool = _Tool("search_knowledge")
        tool.timeout_on = 1  # 第一次调用即超时
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["search_knowledge"], final={"draft_answer": "超时后仍出草稿", "confidence": 0.9}),
            limits=CopilotLimits(single_tool_timeout_seconds=0.1),
        )
        return await copilot.run(_request())

    result = asyncio.run(run())
    assert result["tool_trace"][0]["status"] == "timeout"
    # 超时后模型仍在下一轮给出草稿（不因工具超时抛异常拖垮主流程）
    assert result["draft_answer"] == "超时后仍出草稿"


def test_unregistered_tool_is_denied():
    """模型请求未注册工具：拒绝执行并记录 denied，不崩溃。"""
    async def run():
        tool = _Tool("search_knowledge")
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["send_message"]),  # 模型请求副作用工具
        )
        return await copilot.run(_request())

    result = asyncio.run(run())
    assert result["tool_trace"][0]["status"] == "denied"
    assert result["tool_trace"][0]["reason"] == "unregistered_tool"


class _FakeKnowledge:
    def __init__(self, hits):
        self.hits = hits

    async def lexical_search(self, principal, query, limit=10):
        return [h for h in self.hits if h.tenant_id == principal.tenant_id][:limit]


class _FakeTickets:
    def __init__(self, ticket):
        self.ticket = ticket

    async def get(self, tenant_id, ticket_id):
        return self.ticket if self.ticket.tenant_id == tenant_id else None


class _FakeOps:
    def __init__(self, messages=None):
        self.messages = messages or []

    async def get_ticket_overview(self, tenant_id, ticket_id):
        return {"messages": self.messages}


def test_gate_rejects_missing_citations():
    """无有效引用 -> needs_human_review=true（不可自动回复）。"""
    from backend.copilot.service import CopilotService

    service = CopilotService(None)  # type: ignore[arg-type]
    result = service.apply_gate(
        {
            "draft_answer": "建议重试",
            "troubleshooting_steps": [],
            "citations": [],
            "confidence": 0.95,
            "needs_human_review": False,
        },
        request=_request(),
        allowed_citations=set(),
    )
    assert result.needs_human_review is True
    assert "missing_citations" in result.reason_codes
    assert result.auto_reply is False


def test_gate_rejects_invalid_citation_not_in_allowlist():
    """引用不在白名单（未发布/跨租户/伪造）-> 拒绝该引用并转人工。"""
    from backend.copilot.service import CopilotService

    service = CopilotService(None)  # type: ignore[arg-type]
    result = service.apply_gate(
        {
            "draft_answer": "参考文档处理",
            "citations": [
                {
                    "document_id": "secret-doc",
                    "document_version": 1,
                    "chunk_id": "c1",
                }
            ],
            "confidence": 0.95,
        },
        request=_request(),
        allowed_citations={("other-doc", 1, "c1")},
    )
    assert result.citations == []
    assert "invalid_citation" in result.reason_codes
    assert result.needs_human_review is True


def test_gate_accepts_allowlisted_citation():
    from backend.copilot.service import CopilotService

    service = CopilotService(None)  # type: ignore[arg-type]
    result = service.apply_gate(
        {
            "draft_answer": "参考文档处理",
            "citations": [{"document_id": "vpn-guide", "document_version": 2, "chunk_id": "vpn-03"}],
            "confidence": 0.95,
            "needs_human_review": False,
        },
        request=_request(),
        allowed_citations={("vpn-guide", 2, "vpn-03")},
    )
    assert len(result.citations) == 1
    assert result.citations[0].document_id == "vpn-guide"
    assert result.needs_human_review is False
    assert result.reason_codes == ["gate_passed"]


def test_gate_forces_human_review_on_sensitive_category():
    """finance 类别始终人工复核（即使引用齐全、置信度高）。"""
    from backend.copilot.service import CopilotService

    service = CopilotService(None)  # type: ignore[arg-type]
    result = service.apply_gate(
        {
            "draft_answer": "报销处理",
            "citations": [{"document_id": "fin-1", "document_version": 1, "chunk_id": "c1"}],
            "confidence": 0.99,
            "needs_human_review": False,
        },
        request=_request(category="finance.reimburse"),
        allowed_citations={("fin-1", 1, "c1")},
    )
    assert result.needs_human_review is True
    assert "sensitive_category" in result.reason_codes


def test_gate_forces_human_review_on_low_confidence():
    from backend.copilot.service import CopilotService

    service = CopilotService(None)  # type: ignore[arg-type]
    result = service.apply_gate(
        {
            "draft_answer": "猜测",
            "citations": [{"document_id": "d", "document_version": 1, "chunk_id": "c1"}],
            "confidence": 0.5,
            "needs_human_review": False,
        },
        request=_request(),
        allowed_citations={("d", 1, "c1")},
    )
    assert result.needs_human_review is True
    assert "low_confidence" in result.reason_codes


def test_gate_returns_no_deterministic_conclusion_on_error():
    """工具异常/模型失败 -> 禁止生成确定性结论。"""
    from backend.copilot.service import CopilotService

    service = CopilotService(None)  # type: ignore[arg-type]
    result = service.apply_gate(
        {
            "error_code": "model_failed",
            "tool_trace": [{"tool": "search_knowledge", "status": "failed"}],
        },
        request=_request(),
    )
    assert result.draft_answer is None
    assert result.needs_human_review is True
    assert result.reason_codes == ["model_failed"]
