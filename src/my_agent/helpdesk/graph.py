"""LangGraph intake, clarification, classification, and dispatch workflow."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from langchain_core.runnables import RunnableConfig
from typing_extensions import TypedDict

from .domain import ActorType, ResumeAction
from .intake import (
    IntakePolicy,
    KeywordTicketClassifier,
    TicketCategory,
    TicketClassifier,
    assess_and_dispatch,
    clarification_question,
    missing_required_fields,
    normalize_fields,
)


class HelpdeskIntakeState(TypedDict, total=False):
    ticket_id: str
    requester_id: str
    text: str
    fields: dict[str, Any]
    category: str
    subcategory: str
    classification_signals: list[str]
    classification_needs_review: bool
    missing_fields: list[str]
    clarification_rounds: int
    clarification_exhausted: bool
    dispatch_team_id: str
    priority: str
    risk_level: str
    dispatch_reason_codes: list[str]
    draft_answer: str | None
    citations: list[dict[str, Any]]
    auto_reply: bool
    answer_reason_codes: list[str]
    next: str


def build_helpdesk_intake_graph(
    *,
    classifier: TicketClassifier | None = None,
    policy: IntakePolicy | None = None,
    checkpointer=None,
    rag_service=None,
):
    classifier = classifier or KeywordTicketClassifier()
    policy = policy or IntakePolicy()

    def normalize_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        fields = normalize_fields(state.get("fields") or {})
        if state.get("requester_id") and not fields.get("requester_id"):
            fields["requester_id"] = state["requester_id"]
        return {"fields": fields}

    async def classify_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        result = await classifier.classify(state.get("text", ""), state.get("fields") or {})
        return {
            "category": result.category.value,
            "subcategory": result.subcategory,
            "classification_signals": list(result.signals),
            "classification_needs_review": result.needs_human_review,
        }

    def completeness_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        category = TicketCategory(state.get("category", TicketCategory.OTHER.value))
        missing = missing_required_fields(state.get("fields") or {}, category, policy)
        rounds = int(state.get("clarification_rounds", 0))
        exhausted = bool(missing) and rounds >= policy.clarification_limit(category)
        return {
            "missing_fields": list(missing),
            "clarification_exhausted": exhausted,
            "next": "dispatch" if not missing or exhausted else "clarify",
        }

    def clarify_node(state: HelpdeskIntakeState) -> dict[str, Any]:
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
        if not isinstance(response, dict) or response.get("action") != ResumeAction.PROVIDE_INFORMATION.value:
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
        category = TicketCategory(state.get("category", TicketCategory.OTHER.value))
        decision = assess_and_dispatch(
            text=state.get("text", ""),
            category=category,
            classification_needs_review=bool(state.get("classification_needs_review", False)),
            clarification_exhausted=bool(state.get("clarification_exhausted", False)),
            policy=policy,
        )
        return {
            "dispatch_team_id": decision.team_id,
            "priority": decision.priority,
            "risk_level": decision.risk_level.value,
            "dispatch_reason_codes": list(decision.reason_codes),
            "next": "finish",
        }

    async def compose_answer_node(state: HelpdeskIntakeState, config: RunnableConfig) -> dict[str, Any]:
        if rag_service is None:
            return {}
        from backend.knowledge import RetrievalPrincipal

        tenant_id = str((config.get("configurable") or {}).get("tenant_id") or "")
        decision = await rag_service.answer(
            RetrievalPrincipal(tenant_id=tenant_id, departments=frozenset()),
            state.get("text", ""),
            category=state.get("category", "other"),
            risk_level=state.get("risk_level", "low"),
        )
        return {
            "draft_answer": decision.answer,
            "citations": [item.model_dump(mode="json") for item in decision.citations],
            "auto_reply": decision.auto_reply,
            "answer_reason_codes": list(decision.reason_codes),
        }

    def route_after_completeness(state: HelpdeskIntakeState) -> str:
        return state["next"]

    graph = StateGraph(HelpdeskIntakeState)
    graph.add_node("normalize", normalize_node)
    graph.add_node("classify", classify_node)
    graph.add_node("check_completeness", completeness_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("dispatch", dispatch_node)
    graph.add_node("compose_answer", compose_answer_node)
    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "classify")
    graph.add_edge("classify", "check_completeness")
    graph.add_conditional_edges(
        "check_completeness",
        route_after_completeness,
        {"clarify": "clarify", "dispatch": "dispatch"},
    )
    graph.add_edge("clarify", "check_completeness")
    graph.add_edge("dispatch", "compose_answer")
    graph.add_edge("compose_answer", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())
