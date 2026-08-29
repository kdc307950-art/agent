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
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from backend.copilot.agent import CopilotLimits, ResolutionCopilot, _extract_json
from backend.copilot.models import CopilotRequest
from backend.run_context import RunContext
from backend.tool_governance import ToolGovernance
from src.my_agent.helpdesk import TicketStatus


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


class _FakeAudit:
    def __init__(self):
        self.events = []

    async def record_event(self, context, event_type, **kwargs):
        self.events.append((context.run_id, event_type, kwargs))


class _FakeMetrics:
    """可断言的指标桩：increment 累计计数。"""

    def __init__(self):
        self.counts: dict[str, int] = {}

    def increment(self, name: str, amount: int = 1, attributes=None):
        self.counts[name] = self.counts.get(name, 0) + (amount or 1)


def _runtime(*, scopes=frozenset({"ticket:agent"}), allowed_tools=None, tenant_id="tenant-a"):
    context = RunContext(
        run_id="run-copilot",
        request_id="req-1",
        tenant_id=tenant_id,
        user_id="user-1",
        thread_id="t-1",
        scopes=scopes,
        deadline=asyncio.get_running_loop().time() + 60,
        allowed_tools=allowed_tools,
    )
    audit = _FakeAudit()
    governance = ToolGovernance(audit)
    return SimpleNamespace(
        context=context,
        tool_governance=governance,
        audit=audit,
        metrics=_FakeMetrics(),
    )


def _copilot(tools: dict[str, Any], model, limits: CopilotLimits | None = None):
    return ResolutionCopilot(model=model, tools=tools, limits=limits)


def _run(copilot, request, runtime):
    """执行 Copilot：注入 runtime + 服务端 RunContext（工具治理依赖）。"""
    return copilot.run(request, runtime=runtime, run_context=runtime.context)


def test_extract_json_tolerates_noise():
    assert _extract_json('前文 {"a": 1} 后文') == {"a": 1}
    assert _extract_json("```json\n{\"b\": 2}\n```") == {"b": 2}
    assert _extract_json("no json") == {}


def test_copilot_runs_tool_loop_and_produces_structured_result():
    async def run():
        tool = _Tool("search_knowledge")
        runtime = _runtime()
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["search_knowledge"]),
        )
        return await _run(copilot, _request(), runtime), tool.calls

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
        runtime = _runtime()
        copilot = _copilot(
            {"search_knowledge": tool},
            _AlwaysToolCallModel("search_knowledge"),
            limits=CopilotLimits(
                max_rounds=10, max_tool_calls=2, max_tool_calls_per_round=2
            ),
        )
        return await _run(copilot, _request(), runtime), tool.calls

    result, calls = asyncio.run(run())
    assert len(calls) <= 2
    assert result["error_code"] == "tool_call_limit_exceeded"
    assert result["needs_human_review"] is True


def test_round_limit_terminates_run():
    """达到最大轮次仍未产出结构化结果时标记 round_limit_exceeded。"""
    async def run():
        tool = _Tool("search_knowledge")
        runtime = _runtime()
        copilot = _copilot(
            {"search_knowledge": tool},
            _AlwaysToolCallModel("search_knowledge"),
            limits=CopilotLimits(
                max_rounds=1, max_tool_calls=10, max_tool_calls_per_round=1
            ),
        )
        return await _run(copilot, _request(), runtime)

    result = asyncio.run(run())
    assert result["error_code"] == "round_limit_exceeded"
    assert result["needs_human_review"] is True


def test_single_tool_timeout_does_not_break_run():
    """单工具超时被捕获：工具调用标记 timeout，主流程继续/正常收尾。"""
    async def run():
        tool = _Tool("search_knowledge")
        tool.timeout_on = 1  # 第一次调用即超时
        runtime = _runtime()
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["search_knowledge"], final={"draft_answer": "超时后仍出草稿", "confidence": 0.9}),
            limits=CopilotLimits(single_tool_timeout_seconds=0.1),
        )
        return await _run(copilot, _request(), runtime)

    result = asyncio.run(run())
    assert result["tool_trace"][0]["status"] == "timeout"
    # 超时后模型仍在下一轮给出草稿（不因工具超时抛异常拖垮主流程）
    assert result["draft_answer"] == "超时后仍出草稿"


def test_unregistered_tool_is_denied():
    """模型请求未注册工具：拒绝执行并记录 denied，不崩溃。"""
    async def run():
        tool = _Tool("search_knowledge")
        runtime = _runtime()
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["send_message"]),  # 模型请求副作用工具
        )
        return await _run(copilot, _request(), runtime)

    result = asyncio.run(run())
    assert result["tool_trace"][0]["status"] == "denied"
    assert result["tool_trace"][0]["reason"] == "unregistered_tool"


# ========== 阶段一：工具调用必须经 ToolGovernance ==========


def test_copilot_tool_calls_go_through_governance_and_are_audited():
    """Copilot 工具调用产生治理审计事件（tool_call_started/completed）。"""
    async def run():
        tool = _Tool("search_knowledge")
        runtime = _runtime()
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["search_knowledge"]),
        )
        await _run(copilot, _request(), runtime)
        return runtime.audit.events

    events = asyncio.run(run())
    types = [event[1] for event in events]
    assert "tool_call_started" in types
    assert "tool_call_completed" in types


def test_copilot_tool_denied_by_tenant_allowlist():
    """租户 allowlist 不允许的工具无法调用（治理层拒绝）。"""
    async def run():
        tool = _Tool("search_knowledge")
        # 租户 allowlist 为空：任何工具都被拒绝
        runtime = _runtime()
        governance = ToolGovernance(runtime.audit, tenant_allowlist={"tenant-a": frozenset()})
        runtime.tool_governance = governance
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["search_knowledge"]),
        )
        return await _run(copilot, _request(), runtime)

    result = asyncio.run(run())
    assert result["tool_trace"][0]["status"] == "denied"
    assert "未启用" in result["tool_trace"][0]["reason"] or "不允许" in str(
        result["tool_trace"][0].get("reason", "")
    )


def test_copilot_tool_denied_when_scope_missing():
    """缺少 ticket:agent scope 时工具被拒绝（治理层）。"""
    async def run():
        tool = _Tool("search_knowledge")
        runtime = _runtime(scopes=frozenset({"chat:write"}))  # 无 ticket:agent
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["search_knowledge"]),
        )
        return await _run(copilot, _request(), runtime)

    result = asyncio.run(run())
    assert result["tool_trace"][0]["status"] == "denied"
    assert "权限不足" in result["tool_trace"][0]["reason"]


def test_copilot_forged_send_message_is_denied_by_governance():
    """模型伪造 send_message（注册在 tools 里但 allowed_tools 排除）被治理层拒绝。

    模拟：工具集合包含 send_message，但 RunContext.allowed_tools 只含只读工具；
    治理层 allowed_tools 子集校验必须拦截。
    """
    async def run():
        send_tool = _Tool("send_message")
        runtime = _runtime(
            allowed_tools=frozenset(
                {"search_knowledge", "search_assets", "get_ticket_history", "get_ticket_messages"}
            )
        )
        copilot = _copilot(
            {"send_message": send_tool},
            _ToolCallModel(["send_message"]),
        )
        return await _run(copilot, _request(), runtime), send_tool.calls

    result, calls = asyncio.run(run())
    assert calls == []  # send_message 从未执行
    assert result["tool_trace"][0]["status"] == "denied"


# ========== 阶段四：结构化 error_code（不靠错误文本判断状态） ==========


def test_governance_error_maps_to_structured_error_code():
    """治理拒绝/失败文案映射为结构化 error_code。"""
    from backend.copilot.tool_adapter import _classify_governance_error

    assert _classify_governance_error("工具未注册或不可用") == ("denied_unregistered", "denied")
    assert _classify_governance_error("工具调用权限不足") == ("denied_scope", "denied")
    assert _classify_governance_error("当前租户未启用该工具") == ("denied_tenant", "denied")
    assert _classify_governance_error("工具输入超过允许长度") == ("denied_input", "denied")
    assert _classify_governance_error("工具调用超时") == ("timeout", "timeout")
    assert _classify_governance_error("工具调用失败，请稍后重试") == ("failed", "failed")
    # 未知文案不猜测为 denied
    assert _classify_governance_error("未知错误") == ("failed", "failed")


def test_copilot_tool_trace_includes_structured_error_code():
    """工具被治理拒绝时 tool_trace 带结构化 error_code（而非仅文本）。"""
    async def run():
        tool = _Tool("search_knowledge")
        runtime = _runtime(scopes=frozenset({"chat:write"}))  # 无 ticket:agent
        copilot = _copilot(
            {"search_knowledge": tool},
            _ToolCallModel(["search_knowledge"]),
        )
        return await _run(copilot, _request(), runtime), runtime

    result, runtime = asyncio.run(run())
    trace = result["tool_trace"][0]
    assert trace["status"] == "denied"
    assert trace.get("error_code") == "denied_scope"
    # 可观测性：ACL 拒绝指标（copilot_acl_rejected_total）
    assert runtime.metrics.counts.get("copilot_acl_rejected_total") == 1


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


# ========== 阶段一/二：真实工具证据链路 + 两层门禁（service 层） ==========


class _RealKnowledge:
    """模拟真实 lexical_search：返回命中；verify_citations 做权威校验。

    hits_by_query 支持"补充查询命中不同 chunk"场景：
    查询词 -> 命中列表；None 表示默认全量 hits。
    """

    def __init__(
        self,
        hits,
        verified: set[tuple[str, int, str]] | None = None,
        hits_by_query: dict[str, list[Any]] | None = None,
    ):
        self.hits = hits
        self.hits_by_query = hits_by_query or {}
        # 默认：命中即可验证（与真实 SQL 一致）；测试可注入更严的 verified 集合
        self.verified = verified if verified is not None else {h.key for h in hits}

    async def lexical_search(self, principal, query, limit=10):
        if query in self.hits_by_query:
            return [h for h in self.hits_by_query[query] if h.tenant_id == principal.tenant_id][:limit]
        return [h for h in self.hits if h.tenant_id == principal.tenant_id][:limit]

    async def verify_citations(self, principal, citations):
        from backend.knowledge.models import KnowledgeEvidence

        # 模拟真实 SQL：按 key 查 chunk（真实存在的 chunk 即通过权威校验）。
        # 默认以 self.hits 为准；verified 集合可注入"补充查询命中但未在主 hits"的
        # 额外合法 chunk（对应真实库中 chunk 确实存在）。
        all_hits = {h.key: h for h in self.hits}
        for _, hit in self.hits_by_query.items():
            for h in hit:
                all_hits.setdefault(h.key, h)
        result = []
        for h in all_hits.values():
            if h.tenant_id == principal.tenant_id and h.key in set(citations) and h.key in self.verified:
                result.append(
                    KnowledgeEvidence(
                        document_id=h.document_id,
                        document_version=h.document_version,
                        chunk_id=h.chunk_id,
                        title=h.title,
                        content=h.content,
                    )
                )
        return result


def _hit(doc_id: str, chunk: str = "c1") -> Any:
    from backend.knowledge.models import RetrievalHit

    return RetrievalHit(
        tenant_id="tenant-a",
        document_id=doc_id,
        document_version=1,
        chunk_id=chunk,
        title=f"{doc_id} 标题",
        content=f"{doc_id} 正文",
        source_uri=None,
        source="lexical",
        source_rank=1,
    )


def _service_runtime(knowledge):
    audit = _FakeAudit()
    context = RunContext(
        run_id="run-copilot",
        request_id="req-1",
        tenant_id="tenant-a",
        user_id="user-1",
        thread_id="copilot:tenant-a:t-1",
        scopes=frozenset({"ticket:agent"}),
        deadline=asyncio.get_running_loop().time() + 60,
        allowed_tools=frozenset(
            {"search_knowledge", "search_assets", "get_ticket_history", "get_ticket_messages"}
        ),
    )
    from backend.knowledge.retriever import KnowledgeRetriever

    retriever = KnowledgeRetriever(knowledge)  # 默认 NullVectorRetriever -> lexical-only
    return SimpleNamespace(
        context=context,
        tickets=_FakeTickets(_request_as_ticket()),
        ticket_operations=_FakeOps(),
        knowledge=knowledge,
        knowledge_retriever=retriever,
        tool_governance=ToolGovernance(audit),
        metrics=_FakeMetrics(),
    )


def _request_as_ticket():
    from backend.tickets.models import TicketRecord

    now = datetime.now(UTC)
    return TicketRecord(
        tenant_id="tenant-a",
        ticket_id="t-1",
        requester_id="user-1",
        channel="web",
        external_ticket_id=None,
        title="VPN 无法连接",
        description="客户端无法连接 VPN",
        status=TicketStatus.ASSIGNED,
        priority="normal",
        category="it.vpn",
        asset_id=None,
        assigned_team_id=None,
        assigned_user_id=None,
        version=1,
        metadata={},
        created_at=now,
        updated_at=now,
        resolved_at=None,
        closed_at=None,
    )


class _EvidenceModel:
    """模型第一轮查知识（真实工具证据），第二轮输出引用该证据的 JSON。"""

    def __init__(self, cited: dict | None = None):
        self.cited = cited or {
            "draft_answer": "请重新导入 VPN 配置",
            "troubleshooting_steps": ["重新导入 VPN"],
            "citations": [{"document_id": "vpn-guide", "document_version": 1, "chunk_id": "c1"}],
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
                        "name": "search_knowledge",
                        "args": {"query": "vpn 重新导入"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content=json.dumps(self.cited, ensure_ascii=False))


class _TwoRoundEvidenceModel:
    """模型两轮查知识（主查询 + 补充查询），引用补充查询命中的 chunk。"""

    def __init__(self, cited: dict):
        self.cited = cited
        self.round = 0

    async def ainvoke(self, messages, config=None):
        from langchain_core.messages import AIMessage

        self.round += 1
        if self.round == 1:
            return AIMessage(
                content="先做主查询",
                tool_calls=[
                    {
                        "name": "search_knowledge",
                        "args": {"query": "vpn 排查"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        if self.round == 2:
            return AIMessage(
                content="需要补充查询",
                tool_calls=[
                    {
                        "name": "search_knowledge",
                        "args": {"query": "vpn-03 具体配置"},
                        "id": "call-2",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content=json.dumps(self.cited, ensure_ascii=False))


def test_service_runs_real_tool_evidence_through_two_layer_gate():
    """真实工具证据链路：证据 -> 白名单 -> 权威校验 -> 引用通过（阶段一验收）。"""
    from backend.copilot.agent import ResolutionCopilot
    from backend.copilot.service import CopilotService
    from backend.copilot.tools import search_knowledge

    async def run():
        knowledge = _RealKnowledge([_hit("vpn-guide")])
        runtime = _service_runtime(knowledge)
        # 用真实 Copilot 工具（返回 {content, evidence} 统一契约）
        copilot = ResolutionCopilot(
            model=_EvidenceModel(),
            tools={"search_knowledge": search_knowledge},
        )
        service = CopilotService(copilot)
        outcome = await service.run_with_tenant(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_context=runtime.context,
        )
        return outcome["result"]

    result = asyncio.run(run())
    assert result.citations
    assert result.citations[0].document_id == "vpn-guide"
    assert result.needs_human_review is False
    assert result.auto_reply is False
    # 阶段二：检索模式进入最终结果（lexical-only，未配置 embedding）
    assert result.retrieval_mode == "lexical-only"
    assert result.degraded is False


def test_service_hybrid_mode_marks_retrieval_mode():
    """配置向量检索时：Copilot 检索标记 hybrid（经 KnowledgeRetriever）。"""
    from backend.copilot.agent import ResolutionCopilot
    from backend.copilot.service import CopilotService
    from backend.copilot.tools import search_knowledge
    from backend.knowledge.retriever import KnowledgeRetriever

    async def run():
        knowledge = _RealKnowledge([_hit("vpn-guide")])
        runtime = _service_runtime(knowledge)

        class _FakeVector:
            async def search(self, principal, query, *, limit):
                return []

        # 注入真实向量检索器 -> KnowledgeRetriever 标记 hybrid
        runtime.knowledge_retriever = KnowledgeRetriever(knowledge, _FakeVector())
        copilot = ResolutionCopilot(
            model=_EvidenceModel(),
            tools={"search_knowledge": search_knowledge},
        )
        service = CopilotService(copilot)
        outcome = await service.run_with_tenant(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_context=runtime.context,
        )
        return outcome["result"]

    result = asyncio.run(run())
    assert result.retrieval_mode == "hybrid"
    assert result.degraded is False


def test_service_supplementary_query_hit_passes_gate():
    """Agent 用补充查询命中不同 chunk：引用仍通过（不再误判无效）。"""
    from backend.copilot.agent import ResolutionCopilot
    from backend.copilot.service import CopilotService
    from backend.copilot.tools import search_knowledge

    cited = {
        "draft_answer": "参考 vpn-03 处理",
        "troubleshooting_steps": [],
        "citations": [{"document_id": "vpn-guide", "document_version": 1, "chunk_id": "vpn-03"}],
        "confidence": 0.9,
        "needs_human_review": False,
    }

    async def run():
        # 主查询命中 vpn-01，补充查询命中 vpn-03；两者都是实际工具证据，
        # 权威校验确认 vpn-03 真实存在 -> 引用通过
        knowledge = _RealKnowledge(
            [_hit("vpn-guide", "vpn-01")],
            verified={("vpn-guide", 1, "vpn-03"), ("vpn-guide", 1, "vpn-01")},
            hits_by_query={
                "vpn 排查": [_hit("vpn-guide", "vpn-01")],
                "vpn-03 具体配置": [_hit("vpn-guide", "vpn-03")],
            },
        )
        runtime = _service_runtime(knowledge)
        copilot = ResolutionCopilot(
            model=_TwoRoundEvidenceModel(cited),
            tools={"search_knowledge": search_knowledge},
        )
        service = CopilotService(copilot)
        outcome = await service.run_with_tenant(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_context=runtime.context,
        )
        return outcome["result"]

    result = asyncio.run(run())
    assert [c.chunk_id for c in result.citations] == ["vpn-03"]
    assert result.needs_human_review is False


def test_service_forged_chunk_is_rejected_by_authority_gate():
    """模型伪造不存在的 chunk：权威校验拒绝（第二层门禁）。"""
    from backend.copilot.agent import ResolutionCopilot
    from backend.copilot.service import CopilotService
    from backend.copilot.tools import search_knowledge

    cited = {
        "draft_answer": "伪造引用",
        "citations": [{"document_id": "vpn-guide", "document_version": 1, "chunk_id": "fake-999"}],
        "confidence": 0.99,
        "needs_human_review": False,
    }

    async def run():
        # 工具命中 c1，但模型引用 fake-999；权威校验只放行真实存在的 chunk
        knowledge = _RealKnowledge([_hit("vpn-guide", "c1")])
        runtime = _service_runtime(knowledge)
        copilot = ResolutionCopilot(
            model=_EvidenceModel(cited),
            tools={"search_knowledge": search_knowledge},
        )
        service = CopilotService(copilot)
        outcome = await service.run_with_tenant(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_context=runtime.context,
        )
        return outcome["result"]

    result = asyncio.run(run())
    assert result.citations == []
    assert result.needs_human_review is True
    assert "authority_citation_rejected" in result.reason_codes or "missing_citations" in result.reason_codes


def test_service_authority_rejection_counts_citation_metric():
    """白名单通过但权威校验拒绝：authority_citation_rejected + 指标计数。

    与 test_service_forged_chunk... 的区别：该场景引用不在白名单（第一层拒绝），
    本场景引用在白名单（工具命中）但权威 verify 排除（verified 注入为空），
    走第二层权威门禁的 dropped>0 分支，触发 copilot_citation_rejected_total。
    """
    from backend.copilot.agent import ResolutionCopilot
    from backend.copilot.service import CopilotService
    from backend.copilot.tools import search_knowledge

    async def run():
        # 工具命中 c1 -> 白名单放行；verified=set() -> 权威校验拒绝全部
        knowledge = _RealKnowledge([_hit("vpn-guide", "c1")], verified=set())
        runtime = _service_runtime(knowledge)
        copilot = ResolutionCopilot(
            model=_EvidenceModel(),
            tools={"search_knowledge": search_knowledge},
        )
        service = CopilotService(copilot)
        outcome = await service.run_with_tenant(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_context=runtime.context,
        )
        return outcome["result"], runtime

    result, runtime = asyncio.run(run())
    assert result.citations == []
    assert result.needs_human_review is True
    assert "authority_citation_rejected" in result.reason_codes
    # 可观测性：权威引用拒绝指标（copilot_citation_rejected_total）
    assert runtime.metrics.counts.get("copilot_citation_rejected_total", 0) >= 1


def test_service_no_knowledge_hit_still_produces_draft_with_human_review():
    """无任何知识命中：生成草稿但强制人工复核（无引用）。"""
    from backend.copilot.agent import ResolutionCopilot
    from backend.copilot.service import CopilotService
    from backend.copilot.tools import search_knowledge

    cited = {
        "draft_answer": "暂无知识依据，建议人工排查",
        "troubleshooting_steps": ["人工排查"],
        "citations": [],
        "confidence": 0.5,
        "needs_human_review": True,
    }

    async def run():
        knowledge = _RealKnowledge([])  # 无命中
        runtime = _service_runtime(knowledge)
        copilot = ResolutionCopilot(
            model=_EvidenceModel(cited),
            tools={"search_knowledge": search_knowledge},
        )
        service = CopilotService(copilot)
        outcome = await service.run_with_tenant(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            run_context=runtime.context,
        )
        return outcome["result"]

    result = asyncio.run(run())
    assert result.draft_answer == "暂无知识依据，建议人工排查"  # 草稿仍生成
    assert result.citations == []
    assert result.needs_human_review is True
