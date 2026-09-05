"""VPN 专项评测集 v2（vpn-v2，9 类场景）单元测试。

覆盖：
    - 数据集冻结版本、总数==63、9 类场景每类==7、三边界三态都有分布；
    - VpnEvalCaseV2 新增指标字段（error_code / client_version / network_type /
      fault_hypothesis / acls / risk_level / escalation_expected 等）的 schema 合法性；
    - 各专项约束：ACL 用例 is_negative=True 且 must_escalate；无知识答案
      expected_document_ids=() 且 must_escalate；高风险请求 must_escalate；
      账号锁定-VPN 混淆 期望 it.account 且 must_escalate；769/809 归属 connection_failed；
    - run_vpn_eval --dataset vpn-v2 静态报告：关键指标（零误导向、分类/边界高分）。

注：账号锁定-VPN 混淆样本的期望分类 it.account 是「期望行为」；现有 KeywordTicketClassifier
子分类优先级（vpn 先于 account）对含字面 "VPN/远程接入" 的文本仍归 it.vpn，属已记录的分类器
缺口（见 backend/knowledge/vpn_eval_cases_v2.py 与验收追溯文档 §2-S6），由后续分诊逻辑升级闭合。
"""

import asyncio

from backend.knowledge.vpn_eval_cases_v2 import (
    ALL_SCENARIOS,
    SCENARIO_ACCOUNT_LOCK,
    SCENARIO_ACL,
    SCENARIO_CLIENT_VERSION,
    SCENARIO_ERROR_CODE,
    SCENARIO_HIGH_RISK,
    SCENARIO_NO_KNOWLEDGE,
    V2_EVAL_CASES,
    VPN_EVAL_VERSION_V2,
    boundary_counts,
    count,
    fault_counts,
    scenario_counts,
)
from backend.run_vpn_eval import _run_eval
from src.my_agent.helpdesk import (
    BOUNDARY_AUTO_SUGGEST,
    BOUNDARY_MUST_ASK,
    BOUNDARY_MUST_ESCALATE,
    VPN_FAULT_CONNECTION_FAILED,
    VPN_FAULTS,
)


def test_v2_dataset_is_frozen_with_expected_mix():
    assert VPN_EVAL_VERSION_V2 == "2026-09-05-vpn-v2"
    assert count() == 63
    sc = scenario_counts()
    assert set(sc) == set(ALL_SCENARIOS)
    for name in ALL_SCENARIOS:
        assert sc[name] == 7
    bc = boundary_counts()
    assert set(bc) == {BOUNDARY_AUTO_SUGGEST, BOUNDARY_MUST_ASK, BOUNDARY_MUST_ESCALATE}
    assert bc[BOUNDARY_MUST_ESCALATE] > 0
    fc = fault_counts()
    assert fc["connection_failed"] >= 30
    assert fc["multi_user_impact"] >= 6
    assert fc["negative_out_of_scope"] >= 20
    for case in V2_EVAL_CASES:
        assert case["text"], case
        assert case["vpn_fault"] in VPN_FAULTS
        assert case["expected_category"]
        assert case["expected_team"]
        assert case["expected_boundary"] in (
            BOUNDARY_AUTO_SUGGEST,
            BOUNDARY_MUST_ASK,
            BOUNDARY_MUST_ESCALATE,
        )
        assert len(case["required_fields"]) == 8


def test_v2_every_scenario_at_least_6():
    sc = scenario_counts()
    for name in ALL_SCENARIOS:
        assert sc[name] >= 6, name


def test_v2_new_metric_fields_schema_valid():
    """新增指标字段必须全部存在、类型正确、取值合法。"""
    for case in V2_EVAL_CASES:
        assert "error_code" in case
        assert "client_version" in case
        assert "network_type" in case
        assert case["fault_hypothesis"]
        assert isinstance(case["acls"], tuple)
        assert case["risk_level"] in ("low", "medium", "high")
        # escalation_expected 与边界语义强一致
        assert case["escalation_expected"] is (case["expected_boundary"] == BOUNDARY_MUST_ESCALATE)
        # 快照字段与 provided_fields 一致（允许默认/覆盖，但必须为字符串）
        assert isinstance(case["error_code"], str)
        assert isinstance(case["client_version"], str)
        assert isinstance(case["network_type"], str)


def test_v2_provided_fields_consistent_with_expected_boundary():
    """provided_fields 必须与 expected_boundary 一致：must_ask 必缺字段、
    auto_suggest / must_escalate 必齐 8 项。"""
    for case in V2_EVAL_CASES:
        provided = set((case["provided_fields"] or {}).keys())
        required = set(case["required_fields"])
        complete = required.issubset(provided)
        if case["expected_boundary"] == BOUNDARY_MUST_ASK:
            assert not complete, case["text"]
        else:
            assert complete, case["text"]


def test_v2_acl_out_of_scope_cases_negative_and_escalate():
    acl_cases = [c for c in V2_EVAL_CASES if c["scenario"] == SCENARIO_ACL]
    assert len(acl_cases) >= 6
    for case in acl_cases:
        assert case["is_negative"] is True, case["text"]
        assert case["expected_boundary"] == BOUNDARY_MUST_ESCALATE
        assert case["escalation_expected"] is True
        assert case["internal"] is False
        assert isinstance(case["departments"], tuple)
        assert case["resource"]
        # ACL 越权样本不应自动建议
        assert case["risk_level"] == "high"
        # 越权样本没有知识依据（无文档），且 ACL 作用域收窄为自身租户只读
        assert case["expected_document_ids"] == ()
        assert any("vpn:read" in acl for acl in case["acls"])


def test_v2_no_knowledge_answer_cases():
    nk_cases = [c for c in V2_EVAL_CASES if c["scenario"] == SCENARIO_NO_KNOWLEDGE]
    assert len(nk_cases) >= 6
    for case in nk_cases:
        assert case["expected_document_ids"] == (), case["text"]
        assert case["expected_boundary"] == BOUNDARY_MUST_ESCALATE
        assert case["escalation_expected"] is True
        # 字段齐（无知识答案属于「字段齐但无依据」）
        provided = set((case["provided_fields"] or {}).keys())
        assert set(case["required_fields"]).issubset(provided), case["text"]


def test_v2_high_risk_request_cases():
    hr_cases = [c for c in V2_EVAL_CASES if c["scenario"] == SCENARIO_HIGH_RISK]
    assert len(hr_cases) >= 6
    for case in hr_cases:
        assert case["risk_level"] == "high", case["text"]
        assert case["is_negative"] is True
        assert case["expected_boundary"] == BOUNDARY_MUST_ESCALATE
        assert case["escalation_expected"] is True


def test_v2_account_lockout_vs_vpn_cases():
    lock_cases = [c for c in V2_EVAL_CASES if c["scenario"] == SCENARIO_ACCOUNT_LOCK]
    assert len(lock_cases) >= 6
    for case in lock_cases:
        # 期望：账号锁定误表述为 VPN 故障 -> 归 it.account 且人工升级，不得误导向 it.vpn 自动建议
        assert case["expected_category"] == "it.account", case["text"]
        assert case["is_negative"] is True
        assert case["expected_boundary"] == BOUNDARY_MUST_ESCALATE
        assert case["escalation_expected"] is True
        assert case["expected_document_ids"] == (), case["text"]


def test_v2_error_code_769_809_aligned_with_intake():
    err_cases = [c for c in V2_EVAL_CASES if c["scenario"] == SCENARIO_ERROR_CODE]
    assert len(err_cases) >= 6
    # 769 与 809 语义不同，但都在 intake 的 connection_failed 归口下（与分类函数对齐）
    for case in err_cases:
        code = case["error_code"]
        assert code in ("769", "809", "800"), code
        assert case["vpn_fault"] == VPN_FAULT_CONNECTION_FAILED
        assert case["expected_boundary"] in (
            BOUNDARY_AUTO_SUGGEST,
            BOUNDARY_MUST_ASK,
            BOUNDARY_MUST_ESCALATE,
        )
        assert case["fault_hypothesis"]
    # 769 / 809 必须同时存在且假设不同（体现「区分」语义）
    assert any(c["error_code"] == "769" for c in err_cases)
    assert any(c["error_code"] == "809" for c in err_cases)
    h769 = next(c["fault_hypothesis"] for c in err_cases if c["error_code"] == "769")
    h809 = next(c["fault_hypothesis"] for c in err_cases if c["error_code"] == "809")
    assert h769 != h809


def test_v2_version_outdated_fault_aligned_with_intake():
    ver_cases = [c for c in V2_EVAL_CASES if c["scenario"] == SCENARIO_CLIENT_VERSION]
    assert len(ver_cases) >= 6
    for case in ver_cases:
        assert case["client_version"]
        # 版本过旧文本在 intake 无独立 vpn_fault 信号，按默认归 connection_failed
        assert case["vpn_fault"] == VPN_FAULT_CONNECTION_FAILED, case["text"]
        assert "升级" in case["fault_hypothesis"] or "版本" in case["fault_hypothesis"]


def test_v2_static_report_protection_and_accuracy():
    report = asyncio.run(_run_eval(None, "demo", "vpn-v2"))
    assert report["dataset"] == "vpn_v2"
    assert report["version"] == "2026-09-05-vpn-v2"
    assert report["total"] == 63
    # 关键保护：负向/越权样本不得误导向 it.vpn 自动建议
    assert report["auto_misdirect"]["count"] == 0
    # 故障分类与边界判定高分（确定性组件）
    assert report["classify_vpn_fault"]["accuracy"] >= 0.9
    assert report["boundary"]["accuracy"] >= 0.9
    # 真实 VPN 样本全部进入 it.vpn 主线
    assert report["closed_loop_reachable"]["rate"] == 1.0
    sc = report["scenario_counts"]
    assert set(sc) == set(ALL_SCENARIOS)
    for name in ALL_SCENARIOS:
        assert sc[name] >= 6
    # D6 修复（阶段三）：账号锁定-VPN 混淆已闭环——S6 账号锁定样本现在正确归 it.account，
    # 不再产生 category_mismatch，故 failure_count==0。
    failures = report["failures"]
    assert failures == [], f"意外存在失败项: {failures}"
    assert report["failure_count"] == 0
    assert not any(
        f["scenario"] == SCENARIO_ACCOUNT_LOCK and "category_mismatch" in f["reasons"]
        for f in failures
    )
    # 指标统一段（metrics-engineer 的 M1-M9）也应对 vpn-v2 生效
    metrics = report["metrics"]
    assert metrics["escalation_accuracy"]["recall"] == 1.0
    assert metrics["escalation_accuracy"]["precision"] == 1.0
    assert metrics["high_risk_misdirect"]["misdirect_rate"] == 0.0
    assert metrics["fault_hypothesis_hit"]["hit_rate"] == 1.0
    # 阶段三 M3 真实口径（结构化证据链，不再回退 vpn_fault）的新指标小节存在
    assert metrics["fault_hypothesis_accuracy"]["structural_only"] is True
    assert metrics["evidence_sufficiency"]["sufficiency_rate"] == 1.0
    assert metrics["wrong_escalation"]["wrong_escalation_rate"] == 0.0
    assert metrics["manual_takeover"]["takeover_rate"] > 0.0
    assert metrics["customer_step_completion"]["sample_count"] == 0  # static 无闭环数据
    assert metrics["rediagnosis_success"]["sample_count"] == 0  # static 无闭环数据
