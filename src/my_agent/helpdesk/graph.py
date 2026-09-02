"""工单受理 LangGraph：归一化 → 分类 → 策略 → 完整性 → 澄清/派单 → 拟答。

职责：
    - 把客户提交的工单文本/字段跑完「受理全流程」：
      归一化字段 → 分类（关键词/模型）→ 应用租户 IT 策略 → 检查必填字段
      → 缺信息时 interrupt 追问客户 → 派单（团队/优先级/风险）→ 可选 RAG 拟答
    - 通过 LangGraph checkpointer 支持中断恢复（clarify 节点 interrupt）

关键设计：
    - it_policy_provider 运行时按 tenant_id 动态查询策略，不在启动时预编译，
      支持租户策略热更新；无策略时回退内置 IntakePolicy 默认行为
    - clarify 节点用 interrupt 挂起等待客户补充，resume 后从同一 checkpoint 继续
    - compose_answer 可选：注入 rag_service 后才启用拟答，否则静默跳过
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from .domain import ActorType, ResumeAction
from .intake import (
    IntakePolicy,
    KeywordTicketClassifier,
    TicketCategory,
    TicketClassifier,
    assess_and_dispatch,
    clarification_question,
    normalize_fields,
)


class HelpdeskIntakeState(TypedDict, total=False):
    """受理图的共享状态：字段/文本、分类结果、策略应用结果、派单决策、拟答。

    身份与范围（Day 4）：tenant_id / user_id / departments / asset_id 由
    认证主体或渠道入站事件经 config 注入；缺失时收紧权限并转人工。
    """

    ticket_id: str
    requester_id: str
    text: str
    fields: dict[str, Any]
    category: str
    subcategory: str
    classification_signals: list[str]
    classification_needs_review: bool
    classification_confidence: float
    missing_fields: list[str]
    clarification_rounds: int
    clarification_exhausted: bool
    dispatch_team_id: str
    priority: str
    risk_level: str
    dispatch_reason_codes: list[str]
    # 身份上下文（服务端注入，前端/请求体不可直接提交）
    user_id: str
    departments: list[str]
    asset_id: str
    identity_missing: bool
    # 渠道身份目录无映射（Day 3-4）：空部门/空资产且转人工
    channel_identity_missing: bool
    # ItPolicyProvider 应用结果（动态租户策略，非启动时预编译）
    policy_category: str | None
    policy_required_fields: list[str]
    policy_priority: str | None
    approval_required: bool
    auto_answer_enabled: bool
    draft_answer: str | None
    citations: list[dict[str, Any]]
    auto_reply: bool
    answer_reason_codes: list[str]
    answer_status: str
    next: str


def build_helpdesk_intake_graph(
    *,
    classifier: TicketClassifier | None = None,
    policy: IntakePolicy | None = None,
    checkpointer=None,
    rag_service=None,
    it_policy_provider=None,
):
    """构建受理图。

    it_policy_provider：async get(tenant_id, category) -> TenantItPolicy | None。
    分类完成后按当前 tenant_id 动态查询（it.vpn -> 回退 it -> 默认），
    不在启动时为每个租户预编译图。
    """
    classifier = classifier or KeywordTicketClassifier()
    policy = policy or IntakePolicy()

    def normalize_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """字段归一化：合并/清洗客户字段，补入 requester_id。"""
        fields = normalize_fields(state.get("fields") or {})
        if state.get("requester_id") and not fields.get("requester_id"):
            fields["requester_id"] = state["requester_id"]
        return {"fields": fields}

    async def classify_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """文本分类：产出 category/subcategory/置信度/是否需要人工复核。"""
        result = await classifier.classify(state.get("text", ""), state.get("fields") or {})
        return {
            "category": result.category.value,
            "subcategory": result.subcategory,
            "classification_signals": list(result.signals),
            "classification_needs_review": result.needs_human_review,
            "classification_confidence": result.confidence,
        }

    async def apply_policy_node(
        state: HelpdeskIntakeState, config: RunnableConfig
    ) -> dict[str, Any]:
        """按分类链（子分类 -> 父分类）动态加载租户 IT 策略并写入状态。

        无策略时返回全默认（不强制必填、不要求审批、不自动回答）。
        """
        tenant_id = str((config.get("configurable") or {}).get("tenant_id") or "")
        if not tenant_id or it_policy_provider is None:
            return {
                "policy_category": None,
                "policy_required_fields": [],
                "policy_priority": None,
                "approval_required": False,
                "auto_answer_enabled": False,
            }
        category = state.get("category", "other")
        subcategory = state.get("subcategory") or "general"
        candidates = (
            [category] if subcategory == "general" else [f"{category}.{subcategory}", category]
        )
        found = None
        for key in candidates:
            found = await it_policy_provider.get(tenant_id, key)
            if found is not None:
                break
        if found is None:
            return {
                "policy_category": None,
                "policy_required_fields": [],
                "policy_priority": None,
                "approval_required": False,
                "auto_answer_enabled": False,
            }
        return {
            "policy_category": found.category,
            "policy_required_fields": list(found.required_fields),
            "policy_priority": found.default_priority,
            "approval_required": found.approval_required,
            "auto_answer_enabled": found.auto_answer_enabled,
        }

    def completeness_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """检查必填字段（内置分类要求 ∪ 策略额外要求）；缺失则进入 clarify。"""
        category = TicketCategory(state.get("category", TicketCategory.OTHER.value))
        extra = frozenset(state.get("policy_required_fields") or [])
        fields = state.get("fields") or {}
        missing = tuple(
            sorted(
                name
                for name in (policy.required_fields(category) | extra)
                if fields.get(name) in (None, "", [], {})
            )
        )
        rounds = int(state.get("clarification_rounds", 0))
        exhausted = bool(missing) and rounds >= policy.clarification_limit(category)
        return {
            "missing_fields": list(missing),
            "clarification_exhausted": exhausted,
            "next": "dispatch" if not missing or exhausted else "clarify",
        }

    def clarify_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """追问缺失字段：interrupt 挂起等待客户补充（resume 后从 checkpoint 继续）。

        对响应做三重校验：动作必须是 provide_information、参与者必须是客户、
        提交人必须等于 requester_id，防止他人代填。
        """
        rounds = int(state.get("clarification_rounds", 0))
        response = interrupt(
            {
                "kind": "ticket_clarification",
                "ticket_id": state["ticket_id"],
                "expected_actor": ActorType.CUSTOMER.value,
                "expected_actor_id": state.get("requester_id"),
                "allowed_actions": [ResumeAction.PROVIDE_INFORMATION.value],
                "question": clarification_question(state.get("missing_fields") or []),
            }
        )
        if (
            not isinstance(response, dict)
            or response.get("action") != ResumeAction.PROVIDE_INFORMATION.value
        ):
            raise ValueError("补充信息恢复命令无效")
        if response.get("actor_type") != ActorType.CUSTOMER.value:
            raise ValueError("补充信息必须由客户提交")
        expected_actor_id = state.get("requester_id")
        if expected_actor_id and response.get("actor_id") != expected_actor_id:
            raise ValueError("补充信息提交人不匹配")
        payload = response.get("payload") or {}
        supplied = payload.get("fields") if isinstance(payload, dict) else None
        if not isinstance(supplied, dict):
            raise ValueError("补充信息必须包含 payload.fields")
        fields = dict(state.get("fields") or {})
        fields.update(normalize_fields(supplied))
        return {
            "fields": fields,
            "clarification_rounds": rounds + 1,
        }

    def dispatch_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """派单决策：团队 + 优先级 + 风险等级 + 原因码（叠加策略优先级/审批要求）。"""
        category = TicketCategory(state.get("category", TicketCategory.OTHER.value))
        decision = assess_and_dispatch(
            text=state.get("text", ""),
            category=category,
            classification_needs_review=bool(state.get("classification_needs_review", False)),
            clarification_exhausted=bool(state.get("clarification_exhausted", False)),
            policy=policy,
        )
        priority = state.get("policy_priority") or decision.priority
        reason_codes = list(decision.reason_codes)
        if state.get("approval_required"):
            reason_codes.append("approval_required")
        identity_missing = bool(state.get("channel_identity_missing", False))
        if identity_missing:
            reason_codes.append("channel_identity_missing")
        target_team = decision.team_id
        if identity_missing:
            # 渠道身份目录无映射：即便分类为 IT 也转服务台人工队列
            target_team = policy.team_by_category[TicketCategory.OTHER]
        return {
            "dispatch_team_id": target_team,
            "priority": priority,
            "risk_level": decision.risk_level.value,
            "dispatch_reason_codes": reason_codes,
            "next": "finish",
        }

    async def compose_answer_node(
        state: HelpdeskIntakeState, config: RunnableConfig
    ) -> dict[str, Any]:
        """可选：调用 RAG 服务生成拟答与引用；未注入 rag_service 时跳过。

        主体构造（Day 4）：tenant_id / user_id / departments / asset_id 均从
        config（认证主体或渠道入站事件注入）读取，不使用空部门集；
        身份缺失（无 tenant_id 或 user_id）时收紧权限并转人工：
          - 检索主体 departments 保持空集，internal 由认证主体/渠道身份目录决定
          - answer_status=handoff_high_risk，auto_reply=False，
            answer_reason_codes 追加 identity_missing
        """
        if rag_service is None:
            return {}
        from backend.knowledge import RetrievalPrincipal
        from backend.knowledge.service import answer_status

        configurable = config.get("configurable") or {}
        tenant_id = str(configurable.get("tenant_id") or "")
        user_id = str(configurable.get("user_id") or "")
        raw_departments = configurable.get("departments") or []
        departments = (
            frozenset(str(item) for item in raw_departments)
            if isinstance(raw_departments, (list, tuple, set, frozenset))
            else frozenset()
        )
        identity_missing = bool(state.get("channel_identity_missing", False)) or not bool(
            tenant_id and user_id
        )
        # Day 4：internal 不再固定 False，来自认证主体/渠道身份目录
        principal = RetrievalPrincipal(
            tenant_id=tenant_id or "unknown",
            departments=departments,
            internal=bool(configurable.get("internal", False)),
        )
        decision = await rag_service.answer(
            principal,
            state.get("text", ""),
            category=state.get("category", "other"),
            risk_level=state.get("risk_level", "low"),
        )
        status = answer_status(decision.reason_codes, auto_reply=decision.auto_reply)
        reason_codes = list(decision.reason_codes)
        if identity_missing:
            # 身份缺失：不默认全库/全部门权限，必须转人工
            status = "handoff_high_risk"
            reason_codes.append("identity_missing")
        return {
            "draft_answer": decision.answer,
            "citations": [item.model_dump(mode="json") for item in decision.citations],
            "auto_reply": decision.auto_reply and not identity_missing,
            "answer_reason_codes": reason_codes,
            "answer_status": status,
            "identity_missing": identity_missing,
        }

    def route_after_completeness(state: HelpdeskIntakeState) -> str:
        return state["next"]

    graph = StateGraph(HelpdeskIntakeState)
    graph.add_node("normalize", normalize_node)
    graph.add_node("classify", classify_node)
    graph.add_node("apply_policy", apply_policy_node)
    graph.add_node("check_completeness", completeness_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("dispatch", dispatch_node)
    graph.add_node("compose_answer", compose_answer_node)
    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "classify")
    graph.add_edge("classify", "apply_policy")
    graph.add_edge("apply_policy", "check_completeness")
    # 字段齐全 -> dispatch；缺失且未耗尽追问次数 -> clarify（interrupt 等客户补充）
    graph.add_conditional_edges(
        "check_completeness",
        route_after_completeness,
        {"clarify": "clarify", "dispatch": "dispatch"},
    )
    graph.add_edge("clarify", "check_completeness")
    graph.add_edge("dispatch", "compose_answer")
    graph.add_edge("compose_answer", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())
