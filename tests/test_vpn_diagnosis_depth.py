"""Phase 4 —— VPN 诊断深度（诊断树 / 结论 schema / 护栏）单元测试。

模块归属：tests/test_vpn_diagnosis_depth.py（本任务 t3 新增）。覆盖：
    - 5 类故障（connection_failed / auth_failed / frequent_disconnect /
      intranet_unreachable / multi_user_impact）各产出**互不相同**的正确假设 + 下一步；
    - 结构化诊断树 diagnose_vpn_tree 的每个分支（单用户 account/config/nat/gateway-ok；
      多用户 gateway/region/vendor）；
    - 结论 schema：requires_human 加入 EvidenceDiagnosis，to_dict 对齐目标 6 字段；
    - 护栏 guardrail_evaluate：置信度门槛 / 高风险升级 / 无证据不得自动回复 /
      next_action 自动步骤只在满足条件时给，否则强制转人工；
    - conclusion_fault_class 归并 + 证据引用并入证据链。

全部纯函数、无 IO、无真实模型/DB/VPN 凭证。
"""


from backend.vpn.rules import (
    AccountStatus,
    CONCLUSION_FIELDS,
    GatewayStatus,
    VpnEvidence,
    conclusion_fault_class,
    diagnose_vpn_tree,
    evaluate_evidence,
    guardrail_evaluate,
)


# ========== 1. 5 类故障各得互不相同的正确假设 + 下一步 ==========


def test_five_fault_classes_distinct_hypothesis():
    """5 类故障 intent 必须各产出一个互不相同的假设 + 正确的 next_action。"""
    cases = {
        "connection_failed": (
            VpnEvidence(
                fault="connection_failed",
                account_status=AccountStatus.ACTIVE,
                gateway_status=GatewayStatus.UP,
                error_code="809",
                knowledge_hit=True,
            ),
            "connection_failed",
            "provide_steps",
            False,
        ),
        "auth_failed": (
            VpnEvidence(
                fault="auth_failed",
                account_status=AccountStatus.ACTIVE,
                gateway_status=GatewayStatus.UP,
                auth_attempts=3,
            ),
            "auth_failed",
            "escalate_incident",
            True,
        ),
        "frequent_disconnect": (
            VpnEvidence(
                fault="frequent_disconnect",
                account_status=AccountStatus.ACTIVE,
                gateway_status=GatewayStatus.UP,
                knowledge_hit=True,
            ),
            "frequent_disconnect",
            "provide_steps",
            False,
        ),
        "intranet_unreachable": (
            VpnEvidence(
                fault="intranet_unreachable",
                account_status=AccountStatus.ACTIVE,
                gateway_status=GatewayStatus.UP,
                intranet_signal=True,
                knowledge_hit=True,
            ),
            "intranet_unreachable",
            "provide_steps",
            False,
        ),
        "multi_user_impact": (
            VpnEvidence(fault="multi_user_impact", multi_user_impact=True),
            "multi_user_impact",
            "escalate_incident",
            True,
        ),
    }
    hypotheses = set()
    for fault, (ev, hyp, action, requires_human) in cases.items():
        d = evaluate_evidence(ev)
        assert d.hypothesis == hyp, f"{fault}: {d.hypothesis}"
        assert d.next_action == action, f"{fault}: {d.next_action}"
        assert d.requires_human is requires_human, f"{fault}: {d.requires_human}"
        hypotheses.add(d.hypothesis)
    # 5 类假设必须互不相同（不同的根因假设，不是裸关键词标签的重复）。
    assert len(hypotheses) == 5, hypotheses


def test_auth_failed_is_high_risk_and_human():
    """auth_failed：账号/认证歧义 -> 高层人工，绝不自动给排障步骤。"""
    d = evaluate_evidence(
        VpnEvidence(fault="auth_failed", account_status=AccountStatus.ACTIVE, gateway_status=GatewayStatus.UP)
    )
    assert d.hypothesis == "auth_failed"
    assert d.must_handoff is True
    assert d.requires_human is True
    assert d.next_action == "escalate_incident"
    # 账号锁定被 R2 优先捕获，避免 auth_failed 把账号锁定误判成普通凭据问题。
    d2 = evaluate_evidence(VpnEvidence(fault="auth_failed", account_status=AccountStatus.LOCKED))
    assert d2.hypothesis == "account_locked"


def test_frequent_disconnect_and_intranet_are_distinct():
    d = evaluate_evidence(
        VpnEvidence(
            fault="frequent_disconnect",
            account_status=AccountStatus.ACTIVE,
            gateway_status=GatewayStatus.UP,
            knowledge_hit=True,
        )
    )
    assert d.hypothesis == "frequent_disconnect"
    d2 = evaluate_evidence(
        VpnEvidence(
            fault="intranet_unreachable",
            account_status=AccountStatus.ACTIVE,
            gateway_status=GatewayStatus.UP,
            intranet_signal=True,
            knowledge_hit=True,
        )
    )
    assert d2.hypothesis == "intranet_unreachable"
    assert d.hypothesis != d2.hypothesis


# ========== 2. 结构化诊断树 diagnose_vpn_tree ==========


def test_tree_single_user_account_branch():
    t = diagnose_vpn_tree(VpnEvidence(account_status=AccountStatus.LOCKED))
    assert t.hypothesis == "account_locked"
    assert t.requires_human is True
    assert t.next_action == "escalate_incident"


def test_tree_single_user_config_branch():
    t = diagnose_vpn_tree(
        VpnEvidence(
            account_status=AccountStatus.ACTIVE,
            gateway_status=GatewayStatus.UP,
            client_version="2.9.0",
            required_version="3.4.2",
        )
    )
    assert t.hypothesis == "client_version_outdated"
    assert t.requires_human is False
    assert t.next_action == "provide_steps"


def test_tree_single_user_nat_branch():
    t = diagnose_vpn_tree(
        VpnEvidence(
            fault="intranet_unreachable",
            account_status=AccountStatus.ACTIVE,
            gateway_status=GatewayStatus.UP,
            intranet_signal=True,
            knowledge_hit=True,
        )
    )
    assert t.hypothesis == "local_network_nat"
    assert t.requires_human is False
    assert t.next_action == "provide_steps"


def test_tree_single_user_gateway_no_anomaly_branch():
    t = diagnose_vpn_tree(
        VpnEvidence(account_status=AccountStatus.ACTIVE, gateway_status=GatewayStatus.UP, knowledge_hit=True)
    )
    assert t.hypothesis == "connection_failed"
    assert t.requires_human is False


def test_tree_multi_user_gateway_region_vendor():
    gw = diagnose_vpn_tree(
        VpnEvidence(fault="multi_user_impact", multi_user_impact=True, gateway_status=GatewayStatus.DOWN)
    )
    assert gw.hypothesis == "gateway_down"
    region = diagnose_vpn_tree(
        VpnEvidence(fault="multi_user_impact", multi_user_impact=True, regional_incident=True)
    )
    assert region.hypothesis == "regional_incident"
    vendor = diagnose_vpn_tree(VpnEvidence(fault="multi_user_impact", multi_user_impact=True))
    assert vendor.hypothesis == "vendor_anomaly"


# ========== 3. 结论 schema（requires_human 对齐目标 JSON）==========


def test_conclusion_schema_has_requires_human():
    d = evaluate_evidence(VpnEvidence(account_status=AccountStatus.LOCKED))
    as_dict = d.to_dict()
    assert "requires_human" in as_dict
    assert set(as_dict) == {
        "hypothesis",
        "confidence",
        "evidence",
        "ruled_out",
        "next_action",
        "reason_codes",
        "must_handoff",
        "requires_human",
    }
    # 目标结论 schema 的 6 个关键字段齐全。
    assert all(field in as_dict for field in CONCLUSION_FIELDS)


def test_diagnosis_conclusion_to_dict_goal_shape():
    from backend.vpn.rules import DiagnosisConclusion

    c = DiagnosisConclusion(
        hypothesis="connection_failed",
        confidence=0.85,
        evidence=["a"],
        ruled_out=["b"],
        next_action="provide_steps",
        requires_human=False,
    )
    assert set(c.to_dict()) == set(CONCLUSION_FIELDS)


# ========== 4. 护栏 guardrail_evaluate ==========


def test_guardrail_confidence_gate_forces_human():
    """低置信度（<0.80）不得自动给下一步，必须转人工。"""
    ev = VpnEvidence(
        fault="connection_failed",
        account_status=AccountStatus.ACTIVE,
        gateway_status=GatewayStatus.UP,
        knowledge_hit=False,  # 无知识 -> 置信度 0.6
    )
    c = guardrail_evaluate(ev)
    assert c.hypothesis == "connection_failed"
    assert c.requires_human is True
    assert c.next_action == "escalate_incident"


def test_guardrail_high_risk_auth_multiuser_no_evidence():
    # auth_failed 恒必须人工
    assert guardrail_evaluate(
        VpnEvidence(fault="auth_failed", account_status=AccountStatus.ACTIVE, gateway_status=GatewayStatus.UP)
    ).requires_human is True
    # multi_user 恒必须人工
    assert guardrail_evaluate(VpnEvidence(fault="multi_user_impact", multi_user_impact=True)).requires_human is True
    # 无证据恒必须人工
    assert guardrail_evaluate(VpnEvidence()).requires_human is True


def test_guardrail_no_evidence_never_auto_reply():
    """无证据 -> 绝不自动回复（requires_human=True、next_action=escalate）。"""
    c = guardrail_evaluate(VpnEvidence())
    assert c.hypothesis == "no_evidence"
    assert c.requires_human is True
    assert c.next_action == "escalate_incident"


def test_guardrail_allows_auto_when_confident():
    """置信度足够 + 有证据 + 非高风险 -> 允许自动给排障步骤。"""
    c = guardrail_evaluate(
        VpnEvidence(
            fault="connection_failed",
            account_status=AccountStatus.ACTIVE,
            gateway_status=GatewayStatus.UP,
            knowledge_hit=True,
        )
    )
    assert c.requires_human is False
    assert c.next_action == "provide_steps"


# ========== 5. conclusion_fault_class 归并 + 证据引用 ==========


def test_conclusion_fault_class_mapping():
    assert conclusion_fault_class("account_locked") == "auth_failed"
    assert conclusion_fault_class("gateway_down") == "connection_failed"
    assert conclusion_fault_class("client_version_outdated") == "connection_failed"
    assert conclusion_fault_class("local_network_nat") == "intranet_unreachable"
    assert conclusion_fault_class("regional_incident") == "multi_user_impact"
    assert conclusion_fault_class("vendor_anomaly") == "multi_user_impact"
    assert conclusion_fault_class("auth_failed") == "auth_failed"
    assert conclusion_fault_class("connection_failed") == "connection_failed"


def test_evidence_quote_is_cited():
    """evidence_quote 非空时并入证据链，保证证据引用完整率。"""
    ev = VpnEvidence(
        fault="connection_failed",
        account_status=AccountStatus.ACTIVE,
        gateway_status=GatewayStatus.UP,
        knowledge_hit=True,
        evidence_quote="提示错误码809",
    )
    d = evaluate_evidence(ev)
    assert "提示错误码809" in " ".join(d.evidence)
    assert any("文本引用" in item for item in d.evidence)
