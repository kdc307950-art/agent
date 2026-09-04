"""VPN Diagnosis Agent 契约模型单元测试（backend/vpn/models.py）。

覆盖契约 §1/§2/§3：
    - DiagnosisCommandType 含且仅含 5 个允许命令；
    - FORBIDDEN_COMMANDS 含且仅含 7 个禁止命令；
    - DiagnosisCommand extra="forbid" 拒绝未知字段；
    - DiagnosisCommand 拒绝构造禁止命令（禁止命令永远无法被产出）；
    - evaluate_handoff 四类必须转人工（no_evidence / low_confidence /
      multi_user_impact / identity_missing）可分别触发且原因码正确；
      无这些情形时 must_handoff=False。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.vpn import (
    ALLOWED_COMMANDS,
    DEFAULT_HANDOFF_CONFIDENCE,
    FORBIDDEN_COMMANDS,
    DiagnosisCommand,
    DiagnosisCommandType,
    HandoffReason,
    evaluate_handoff,
)


def test_diagnosis_command_type_has_exactly_five_allowed():
    """DiagnosisCommandType 含且仅含 5 个允许命令（契约 §1）。"""
    expected = {
        "ask_customer",
        "provide_steps",
        "assign_agent",
        "escalate_incident",
        "request_approval",
    }
    values = {command.value for command in DiagnosisCommandType}
    assert values == expected
    assert len(list(DiagnosisCommandType)) == 5
    assert ALLOWED_COMMANDS == expected


def test_forbidden_commands_has_exactly_seven():
    """FORBIDDEN_COMMANDS 含且仅含 7 个禁止命令（契约 §1.1）。"""
    expected = {
        "reset_password",
        "unlock_account",
        "grant_vpn_permission",
        "modify_vpn_config",
        "restart_gateway",
        "close_ticket",
        "send_customer_message",
    }
    assert FORBIDDEN_COMMANDS == frozenset(expected)
    assert len(FORBIDDEN_COMMANDS) == 7


def test_forbidden_and_allowed_are_disjoint():
    assert not (FORBIDDEN_COMMANDS & ALLOWED_COMMANDS)


def test_diagnosis_command_rejects_unknown_fields():
    """extra="forbid"：模型文本直出的任意业务字段必须被拒绝。"""
    with pytest.raises(ValidationError):
        DiagnosisCommand(
            command=DiagnosisCommandType.PROVIDE_STEPS,
            content="请重启客户端",
            payload={},
            reason_codes=[],
            confidence=0.95,
            some_unknown_field="x",  # 未知字段：extra="forbid" 应拒绝
        )


def test_diagnosis_command_rejects_confidence_out_of_range():
    with pytest.raises(ValidationError):
        DiagnosisCommand(
            command=DiagnosisCommandType.ASK_CUSTOMER,
            content="追问",
            payload={},
            confidence=1.5,
        )
    with pytest.raises(ValidationError):
        DiagnosisCommand(
            command=DiagnosisCommandType.ASK_CUSTOMER,
            content="追问",
            payload={},
            confidence=-0.1,
        )


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_COMMANDS))
def test_forbidden_command_cannot_be_constructed(forbidden):
    """禁止命令永远不能由 agent 产出：构造即被模型层拒绝（零副作用底线）。"""
    with pytest.raises(ValidationError):
        DiagnosisCommand(
            command=forbidden,  # 非 DiagnosisCommandType 允许值 -> 模型校验拒绝
            content="越权操作",
            payload={},
            confidence=0.9,
        )


def test_diagnosis_command_accepts_all_allowed():
    """5 个允许命令都能构造成功。"""
    for command in DiagnosisCommandType:
        cmd = DiagnosisCommand(
            command=command,
            content="命令文本",
            payload={"k": "v"},
            reason_codes=["x"],
            confidence=0.9,
        )
        assert cmd.command == command
        assert cmd.payload == {"k": "v"}


# ========== evaluate_handoff：四类必须转人工 ==========


def _evidence_present():
    """可支撑结论的依据：非空 struct 带业务字段（_has_evidence 视为 True）。"""
    return {"found": False or {"document_id": "vpn-001"}}


def test_handoff_no_evidence():
    """无证据 -> must_handoff=True，原因码含 no_evidence。"""
    result = evaluate_handoff(
        evidence=[],
        fault="connection_failed",
        confidence=0.95,
        identity_ok=True,
    )
    assert result.must_handoff is True
    assert HandoffReason.NO_EVIDENCE in result.handoff_reasons
    assert result.evidence_found is False


def test_handoff_low_confidence():
    """低置信度（低于阈值）-> must_handoff=True，原因码含 low_confidence。"""
    result = evaluate_handoff(
        evidence=_evidence_present(),
        fault="connection_failed",
        confidence=DEFAULT_HANDOFF_CONFIDENCE - 0.01,
        identity_ok=True,
    )
    assert result.must_handoff is True
    assert HandoffReason.LOW_CONFIDENCE in result.handoff_reasons


def test_handoff_multi_user_impact():
    """多人故障 -> must_handoff=True，原因码含 multi_user_impact。"""
    result = evaluate_handoff(
        evidence=_evidence_present(),
        fault="multi_user_impact",
        confidence=0.95,
        identity_ok=True,
    )
    assert result.must_handoff is True
    assert HandoffReason.MULTI_USER_IMPACT in result.handoff_reasons


def test_handoff_identity_missing():
    """身份缺失 -> must_handoff=True，原因码含 identity_missing。"""
    result = evaluate_handoff(
        evidence=_evidence_present(),
        fault="connection_failed",
        confidence=0.95,
        identity_ok=False,
    )
    assert result.must_handoff is True
    assert HandoffReason.IDENTITY_MISSING in result.handoff_reasons


def test_handoff_none_of_the_four_conditions():
    """无任何升级情形 -> must_handoff=False，handoff_reasons=[]。"""
    result = evaluate_handoff(
        evidence=_evidence_present(),
        fault="connection_failed",
        confidence=0.95,
        identity_ok=True,
    )
    assert result.must_handoff is False
    assert result.handoff_reasons == []


def test_handoff_multiple_reasons_stack():
    """多个情形同时命中：多原因码叠加（如无证据 + 身份缺失）。"""
    result = evaluate_handoff(
        evidence=[],
        fault="multi_user_impact",
        confidence=0.1,
        identity_ok=False,
    )
    assert result.must_handoff is True
    reasons = set(result.handoff_reasons)
    assert {
        HandoffReason.NO_EVIDENCE,
        HandoffReason.LOW_CONFIDENCE,
        HandoffReason.MULTI_USER_IMPACT,
        HandoffReason.IDENTITY_MISSING,
    }.issubset(reasons)


def test_handoff_high_confidence_equals_threshold_is_ok():
    """置信度恰好等于阈值视为通过（不低于），不触发 low_confidence。"""
    result = evaluate_handoff(
        evidence=_evidence_present(),
        fault="connection_failed",
        confidence=DEFAULT_HANDOFF_CONFIDENCE,
        identity_ok=True,
    )
    assert HandoffReason.LOW_CONFIDENCE not in result.handoff_reasons
    assert result.must_handoff is False
