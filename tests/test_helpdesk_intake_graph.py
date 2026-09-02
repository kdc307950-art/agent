import asyncio
from types import SimpleNamespace
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
    assert set(result["dispatch_reason_codes"]) == {
        "classification_review",
        "unknown_category",
        "out_of_scope_manual_review",
    }


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


# ========== V1 范围（Day 2）：越界分类不自动处置 ==========


@pytest.mark.parametrize(
    ("category", "text", "required"),
    [
        (TicketCategory.FINANCE, "报销发票付款流程咨询", {"finance_topic": "报销"}),
        (TicketCategory.ADMIN, "会议室门禁工位申请", {"request_type": "工位"}),
        (TicketCategory.PRODUCT, "产品页面订单功能问题", {"product_name": "订单页", "impact": "无法下单"}),
        (TicketCategory.OTHER, "我需要一些帮助", {}),
    ],
)
def test_out_of_scope_category_routes_to_service_desk_manual_queue(category, text, required):
    """非 IT 大类（finance/admin/product/other）命中后统一转服务台人工队列。

    不得自动派至 team-finance / team-admin / team-product / 业务团队。
    """
    classifier = FixedClassifier(category, needs_review=False, confidence=0.9)
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())
    fields = {
        "title": "咨询",
        "description": text,
        "requester_id": "customer-1",
        **required,
    }

    result = invoke(graph, base_input(text=text, fields=fields), config())

    assert result["dispatch_team_id"] == "team-service-desk"
    assert "out_of_scope_manual_review" in result["dispatch_reason_codes"]
    assert result["dispatch_team_id"] != "team-finance"
    assert result["dispatch_team_id"] != "team-admin"
    assert result["dispatch_team_id"] != "team-product"


def test_it_category_still_auto_dispatches_to_it_team():
    """V1 范围内（IT）仍按分类正常自动派单，不追加越界原因码。"""
    classifier = FixedClassifier(TicketCategory.IT)
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())

    result = invoke(graph, base_input(text="VPN 无法连接"), config())

    assert result["dispatch_team_id"] == "team-it"
    assert "out_of_scope_manual_review" not in result["dispatch_reason_codes"]


# ========== Day 4：身份/部门/资产上下文注入与缺失闭锁 ==========


class FakeRagService:
    def __init__(self):
        self.principals = []

    async def answer(self, principal, question, *, category, risk_level):
        self.principals.append(principal)
        return SimpleNamespace(
            answer="建议先重启 VPN 客户端",
            citations=(),
            auto_reply=True,
            reason_codes=("gate_passed",),
        )


def test_compose_answer_receives_identity_departments_from_config():
    """受理图必须把 config 注入的 user/departments 传给 RAG 主体，而非空部门集。"""
    rag = FakeRagService()
    classifier = FixedClassifier(TicketCategory.IT, subcategory="vpn")
    graph = build_helpdesk_intake_graph(
        classifier=classifier, checkpointer=MemorySaver(), rag_service=rag
    )
    run_config = config()
    run_config["configurable"]["tenant_id"] = "tenant-a"
    run_config["configurable"]["user_id"] = "customer-1"
    run_config["configurable"]["departments"] = ["it"]

    result = invoke(graph, base_input(), run_config)

    assert rag.principals[0].tenant_id == "tenant-a"
    assert rag.principals[0].departments == frozenset({"it"})
    assert rag.principals[0].internal is False
    assert result["answer_status"] == "draft_ready"
    assert result["auto_reply"] is True


def test_missing_identity_tightens_permissions_and_hands_off():
    """无身份时不得默认全库检索权限：空部门 + internal=False + 转人工。"""
    rag = FakeRagService()
    classifier = FixedClassifier(TicketCategory.IT, subcategory="vpn")
    graph = build_helpdesk_intake_graph(
        classifier=classifier, checkpointer=MemorySaver(), rag_service=rag
    )
    run_config = config()
    run_config["configurable"]["tenant_id"] = "tenant-a"
    # 故意不注入 user_id（身份缺失）

    result = invoke(graph, base_input(), run_config)

    assert rag.principals[0].tenant_id == "tenant-a"
    assert rag.principals[0].departments == frozenset()
    assert rag.principals[0].internal is False
    assert result["answer_status"] == "handoff_high_risk"
    assert result["auto_reply"] is False
    assert "identity_missing" in result["answer_reason_codes"]


def test_channel_identity_missing_routes_to_service_desk_manual_queue():
    """Day 3-4：渠道身份目录无映射时，即便分类为 IT 也转服务台人工队列。"""
    classifier = FixedClassifier(TicketCategory.IT, subcategory="vpn")
    graph = build_helpdesk_intake_graph(classifier=classifier, checkpointer=MemorySaver())

    result = invoke(
        graph,
        base_input(text="VPN 无法连接", channel_identity_missing=True),
        config(),
    )

    assert result["dispatch_team_id"] == "team-service-desk"
    assert "channel_identity_missing" in result["dispatch_reason_codes"]
