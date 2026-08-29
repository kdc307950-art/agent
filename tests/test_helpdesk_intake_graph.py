import asyncio
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.my_agent.helpdesk import (
    ClassificationResult,
    IntakePolicy,
    RiskLevel,
    TicketCategory,
    build_helpdesk_intake_graph,
)


class FixedClassifier:
    def __init__(
        self,
        category: TicketCategory,
        *,
        needs_review: bool = False,
        subcategory: str = "general",
        confidence: float = 0.9,
    ):
        self.result = ClassificationResult(
            category=category,
            subcategory=subcategory,
            signals=("test",),
            needs_human_review=needs_review,
            confidence=confidence,
        )
        self.calls = 0

    async def classify(self, text, fields):
        self.calls += 1
        return self.result


def config():
    return {"configurable": {"thread_id": f"intake-{uuid4().hex}"}}


def invoke(graph, inputs, run_config):
    return asyncio.run(graph.ainvoke(inputs, run_config))


def get_state(graph, run_config):
    return asyncio.run(graph.aget_state(run_config))


def base_input(**overrides):
    state = {
        "ticket_id": "ticket-1",
        "requester_id": "customer-1",
        "text": "公司 SSO 登录故障",
        "fields": {
            "title": "Cannot sign in",
            "description": "SSO returns an error",
            "affected_system": "SSO",
            "impact": "one user",
        },
        "clarification_rounds": 0,
    }
    state.update(overrides)
    return state


def test_complete_ticket_is_classified_and_dispatched_without_interrupt():
    classifier = FixedClassifier(TicketCategory.IT)
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())

    result = invoke(graph, base_input(), config())

    assert classifier.calls == 1
    assert result["category"] == "it"
    assert result["dispatch_team_id"] == "team-it"
    assert result["priority"] == "normal"
    assert result["risk_level"] == "low"
    assert result["dispatch_reason_codes"] == ["category_rule"]
    assert result["next"] == "finish"


def test_missing_fields_interrupt_is_customer_scoped_then_resumes():
    classifier = FixedClassifier(TicketCategory.IT)
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())
    run_config = config()
    initial = base_input(fields={"title": "Login error", "description": "Cannot sign in"})

    first = invoke(graph, initial, run_config)
    assert "__interrupt__" in first
    snapshot = get_state(graph, run_config)
    pending = snapshot.tasks[0].interrupts[0]
    assert pending.value["kind"] == "ticket_clarification"
    assert pending.value["expected_actor"] == "customer"
    assert pending.value["expected_actor_id"] == "customer-1"
    assert pending.value["allowed_actions"] == ["provide_information"]
    assert pending.value["ticket_id"] == "ticket-1"
    assert pending.value["question"] == "请补充以下信息：affected_system、impact"

    result = invoke(
        graph,
        Command(
            resume={
                "action": "provide_information",
                "actor_type": "customer",
                "actor_id": "customer-1",
                "payload": {"fields": {"affected_system": "SSO", "impact": "one user"}},
            }
        ),
        run_config,
    )

    assert result["clarification_rounds"] == 1
    assert result["missing_fields"] == []
    assert result["dispatch_team_id"] == "team-it"
    assert classifier.calls == 1


def test_clarification_limit_is_policy_driven_and_exhaustion_dispatches():
    classifier = FixedClassifier(TicketCategory.IT)
    policy = IntakePolicy(max_clarification_rounds={TicketCategory.IT: 1})
    graph = build_helpdesk_intake_graph(
        classifier=classifier,
        policy=policy,
        checkpointer=MemorySaver(),
    )
    run_config = config()
    invoke(graph, base_input(fields={"title": "Login", "description": "Broken"}), run_config)

    result = invoke(
        graph,
        Command(
            resume={
                "action": "provide_information",
                "actor_type": "customer",
                "actor_id": "customer-1",
                "payload": {"fields": {"affected_system": "SSO"}},
            }
        ),
        run_config,
    )

    assert result["clarification_exhausted"] is True
    assert result["missing_fields"] == ["impact"]
    assert result["dispatch_team_id"] == "team-it"
    assert "clarification_exhausted" in result["dispatch_reason_codes"]


def test_high_impact_and_sensitive_text_routes_urgent_high_risk():
    classifier = FixedClassifier(TicketCategory.IT)
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())

    result = invoke(
        graph,
        base_input(text="生产环境全部用户无法办公，需要开通权限"),
        config(),
    )

    assert result["priority"] == "urgent"
    assert result["risk_level"] == RiskLevel.HIGH.value
    assert set(result["dispatch_reason_codes"]) == {"sensitive_operation", "high_impact"}


def test_unknown_or_ambiguous_classification_forces_review_reason():
    classifier = FixedClassifier(TicketCategory.OTHER, needs_review=True)
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())
    state = base_input(
        text="I need help",
        fields={
            "title": "Help",
            "description": "Unclear request",
            "requester_id": "customer-1",
        },
    )

    result = invoke(graph, state, config())

    assert result["dispatch_team_id"] == "team-service-desk"
    assert set(result["dispatch_reason_codes"]) == {"classification_review", "unknown_category"}


@pytest.mark.parametrize(
    ("resume", "message"),
    [
        ({"action": "approve", "payload": {}}, "恢复命令无效"),
        (
            {
                "action": "provide_information",
                "actor_type": "agent",
                "actor_id": "agent-1",
                "payload": {"fields": {}},
            },
            "必须由客户",
        ),
        (
            {
                "action": "provide_information",
                "actor_type": "customer",
                "actor_id": "customer-2",
                "payload": {"fields": {}},
            },
            "提交人不匹配",
        ),
    ],
)
def test_invalid_clarification_resume_payload_is_rejected(resume, message):
    classifier = FixedClassifier(TicketCategory.IT)
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())
    run_config = config()
    invoke(graph, base_input(fields={"title": "Login", "description": "Broken"}), run_config)

    with pytest.raises(ValueError, match=message):
        invoke(graph, Command(resume=resume), run_config)


# ========== 第三阶段：ItPolicyProvider 动态策略 ==========


class FakeItPolicyProvider:
    def __init__(self, policies: dict[str, dict]):
        self.policies = policies
        self.calls = []

    async def get(self, tenant_id, category):
        self.calls.append((tenant_id, category))
        item = self.policies.get(category)
        if item is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(
            category=category,
            required_fields=item.get("required_fields", ()),
            default_priority=item.get("default_priority", "normal"),
            approval_required=item.get("approval_required", False),
            auto_answer_enabled=item.get("auto_answer_enabled", False),
        )


def test_it_policy_provider_applies_required_fields_priority_and_approval():
    provider = FakeItPolicyProvider(
        {
            "it.vpn": {
                "required_fields": ("vpn_account",),
                "default_priority": "high",
                "approval_required": True,
            }
        }
    )
    classifier = FixedClassifier(TicketCategory.IT, subcategory="vpn")
    graph = build_helpdesk_intake_graph(
        classifier=classifier,
        checkpointer=MemorySaver(),
        it_policy_provider=provider,
    )
    run_config = config()
    run_config["configurable"]["tenant_id"] = "tenant-a"

    first = invoke(graph, base_input(), run_config)
    assert "__interrupt__" in first
    assert first["missing_fields"] == ["vpn_account"]

    resume_input = {
        "action": "provide_information",
        "actor_type": "customer",
        "actor_id": "customer-1",
        "payload": {"fields": {"vpn_account": "zhang.san"}},
    }
    result = invoke(graph, Command(resume=resume_input), run_config)
    assert result["priority"] == "high"
    assert "approval_required" in result["dispatch_reason_codes"]
    assert ("tenant-a", "it.vpn") in provider.calls


def test_it_policy_falls_back_to_parent_category():
    provider = FakeItPolicyProvider({"it": {"default_priority": "urgent"}})
    classifier = FixedClassifier(TicketCategory.IT, subcategory="vpn")
    graph = build_helpdesk_intake_graph(
        classifier=classifier,
        checkpointer=MemorySaver(),
        it_policy_provider=provider,
    )
    run_config = config()
    run_config["configurable"]["tenant_id"] = "tenant-a"

    result = invoke(graph, base_input(), run_config)
    assert result["priority"] == "urgent"
    assert ("tenant-a", "it.vpn") in provider.calls
    assert ("tenant-a", "it") in provider.calls


def test_it_policy_provider_absent_uses_defaults():
    classifier = FixedClassifier(TicketCategory.IT, subcategory="vpn")
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())
    run_config = config()
    run_config["configurable"]["tenant_id"] = "tenant-a"

    result = invoke(graph, base_input(), run_config)
    assert result["priority"] == "normal"
    assert "approval_required" not in result["dispatch_reason_codes"]
