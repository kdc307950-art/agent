"""VPN 专项评测集与运行器（vpn-v1）单元测试。

覆盖：
    - 数据集冻结版本、总数==60、五类 vpn_fault 与三边界均有分布；
    - run_vpn_eval 静态报告：vpn_fault 分类准确率、字段补全率、负向误导向=0；
    - boundary_vpn / classify_vpn_fault 的单元行为。
"""

import asyncio

import pytest

from backend.knowledge.vpn_eval_cases import (
    VPN_EVAL_CASES,
    VPN_EVAL_VERSION,
    boundary_counts,
    count,
    fault_counts,
)
from backend.run_vpn_eval import _run_eval
from src.my_agent.helpdesk import (
    BOUNDARY_AUTO_SUGGEST,
    BOUNDARY_MUST_ASK,
    BOUNDARY_MUST_ESCALATE,
    VPN_FAULT_AUTH_FAILED,
    VPN_FAULT_CONNECTION_FAILED,
    VPN_FAULT_FREQUENT_DISCONNECT,
    VPN_FAULT_INTRANET_UNREACHABLE,
    VPN_FAULT_MULTI_USER_IMPACT,
    VPN_FAULTS,
    boundary_vpn,
    classify_vpn_fault,
)


def test_vpn_eval_dataset_is_frozen_with_expected_mix():
    assert VPN_EVAL_VERSION == "2026-09-05-vpn-v1"
    assert count() == 60
    fc = fault_counts()
    assert fc == {
        "connection_failed": 12,
        "frequent_disconnect": 10,
        "auth_failed": 10,
        "intranet_unreachable": 10,
        "multi_user_impact": 8,
        "negative_out_of_scope": 10,
    }
    bc = boundary_counts()
    # 三边界都必须有分布
    assert set(bc) == {BOUNDARY_AUTO_SUGGEST, BOUNDARY_MUST_ASK, BOUNDARY_MUST_ESCALATE}
    for case in VPN_EVAL_CASES:
        assert case["text"]
        assert case["vpn_fault"] in VPN_FAULTS
        assert case["expected_category"]
        assert case["expected_team"]
        assert case["expected_boundary"] in (
            BOUNDARY_AUTO_SUGGEST,
            BOUNDARY_MUST_ASK,
            BOUNDARY_MUST_ESCALATE,
        )
        assert len(case["required_fields"]) == 8


def test_provided_fields_consistent_with_expected_boundary():
    """provided_fields 必须与 expected_boundary 一致：must_ask 必缺字段、
    auto_suggest/must_escalate 必齐 8 项。"""
    for case in VPN_EVAL_CASES:
        provided = set((case["provided_fields"] or {}).keys())
        required = set(case["required_fields"])
        complete = required.issubset(provided)
        if case["expected_boundary"] == BOUNDARY_MUST_ASK:
            assert not complete, case["text"]
        else:
            assert complete, case["text"]


def test_vpn_eval_static_report():
    report = asyncio.run(_run_eval(None, "demo"))
    assert report["total"] == 60
    assert report["classify_vpn_fault"]["accuracy"] >= 0.9
    assert report["field_completion"]["detection_rate"] >= 0.95
    assert report["boundary"]["accuracy"] >= 0.9
    assert report["auto_misdirect"]["count"] == 0
    assert report["closed_loop_reachable"]["rate"] == 1.0
    assert report["knowledge"]["mode"] == "static"
    assert report["knowledge"]["reference_support_rate"] is None
    assert report["failure_count"] == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("VPN 认证失败，用户名或密码错误", VPN_FAULT_AUTH_FAILED),
        ("全公司都无法连接 VPN", VPN_FAULT_MULTI_USER_IMPACT),
        ("VPN 连不上，提示错误码 809", VPN_FAULT_CONNECTION_FAILED),
        ("VPN 频繁掉线", VPN_FAULT_FREQUENT_DISCONNECT),
        ("VPN 能连上但访问不了内网", VPN_FAULT_INTRANET_UNREACHABLE),
        ("一句与 VPN 无关的话", VPN_FAULT_CONNECTION_FAILED),
    ],
)
def test_classify_vpn_fault(text, expected):
    assert classify_vpn_fault(text) == expected


def test_boundary_vpn_decisions():
    # 字段齐 + 无风险 + 有依据 -> auto_suggest
    assert (
        boundary_vpn(VPN_FAULT_CONNECTION_FAILED, True, False, False, True) == BOUNDARY_AUTO_SUGGEST
    )
    # auth_failed 高风险必须人工升级
    assert boundary_vpn(VPN_FAULT_AUTH_FAILED, True, False, False, True) == BOUNDARY_MUST_ESCALATE
    # multi_user_impact 群体故障必须人工升级
    assert (
        boundary_vpn(VPN_FAULT_MULTI_USER_IMPACT, True, False, False, True)
        == BOUNDARY_MUST_ESCALATE
    )
    # 字段不全 -> 先追问
    assert boundary_vpn(VPN_FAULT_CONNECTION_FAILED, False, False, False, True) == BOUNDARY_MUST_ASK
    # 命中敏感词/高影响词 -> 升级
    assert (
        boundary_vpn(VPN_FAULT_CONNECTION_FAILED, True, True, False, True) == BOUNDARY_MUST_ESCALATE
    )
    assert (
        boundary_vpn(VPN_FAULT_CONNECTION_FAILED, True, False, True, True) == BOUNDARY_MUST_ESCALATE
    )
    # 无知识依据 -> 升级（无证据不自动建议）
    assert (
        boundary_vpn(VPN_FAULT_CONNECTION_FAILED, True, False, False, False)
        == BOUNDARY_MUST_ESCALATE
    )
    # 字段不全时 auth_failed 也先追问（决策函数顺序：字段 > 认证）
    assert boundary_vpn(VPN_FAULT_AUTH_FAILED, False, False, False, True) == BOUNDARY_MUST_ASK
