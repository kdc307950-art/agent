"""V1 固定工单评测集与运行器（Day 3）单元测试。"""

import asyncio

from backend.knowledge.ticket_eval_cases import (
    TICKET_EVAL_CASES,
    TICKET_EVAL_VERSION,
    ticket_eval_case_count,
    ticket_eval_scenario_counts,
)
from backend.run_ticket_eval import _run_eval


def test_ticket_eval_dataset_is_frozen_with_expected_scenario_mix():
    assert TICKET_EVAL_VERSION == "2026-09-12-v1"
    assert ticket_eval_case_count() == 90
    counts = ticket_eval_scenario_counts()
    assert counts == {
        "vpn": 30,
        "account": 20,
        "network": 20,
        "fields_missing": 10,
        "no_knowledge": 5,
        "acl": 5,
    }
    for case in TICKET_EVAL_CASES:
        assert case["text"]
        assert case["expected_category"]
        assert "expected_team" in case
        assert "expected_human_takeover" in case


def test_ticket_eval_static_report_meets_v1_gates():
    report = asyncio.run(_run_eval(None, "demo"))
    assert report["total"] == 90
    assert report["classification"]["top1"] >= 0.9
    assert report["field_completion"]["detection_rate"] >= 0.95
    assert report["manual_handoff"]["policy_ok_rate"] == 1.0
    assert report["knowledge"]["acl_leaks"] == 0
    assert report["failure_count"] == 0
