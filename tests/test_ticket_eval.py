"""V1 固定工单评测集与运行器（Day 3 / Day 5-6）单元测试。"""

import asyncio
import sys

import pytest

from backend.knowledge.ticket_eval_cases import (
    TICKET_EVAL_CASES,
    TICKET_EVAL_VERSION,
    ticket_eval_case_count,
    ticket_eval_scenario_counts,
)
from backend.run_ticket_eval import _run_eval, main


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


def test_ticket_eval_static_report_is_na_for_reference_and_acl():
    """static 模式不产生引用/ACL 指标，也不把它们当作通过判定。"""
    report = asyncio.run(_run_eval(None, "demo"))
    assert report["total"] == 90
    assert report["classification"]["top1"] >= 0.9
    assert report["field_completion"]["detection_rate"] >= 0.95
    assert report["manual_handoff"]["policy_ok_rate"] == 1.0
    assert report["knowledge"]["mode"] == "static"
    assert report["knowledge"]["reference_support_rate"] is None
    assert report["knowledge"]["acl_leaks"] is None
    assert report["failure_count"] == 0


def test_require_db_flag_rejects_static_mode(monkeypatch):
    """--require-db 未配置数据库连接时必须拒绝，禁止把 static 当 db 评测。"""
    # 防御：全量测试中其他用例可能残留数据库环境变量，这里显式清空。
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["backend.run_ticket_eval", "--require-db"],
    )
    with pytest.raises(SystemExit, match="--require-db"):
        main()
