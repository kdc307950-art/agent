"""VPN 确定性证据链诊断规则单元测试（backend/vpn/rules.py，阶段三）。

覆盖：
    - 信号归一化（normalize_account_status / normalize_gateway_status / 版本比较）；
    - 规则表 R0-R6 每一条（身份缺失 / 多用户 / 账号锁定 / 客户端版本 / 网关侧 / 无证据 / 连接兜底）；
    - 结构化诊断结论字段（hypothesis / confidence / evidence[] / ruled_out[] /
      next_action / reason_codes[] / must_handoff）——M3 真实口径的载体；
    - 主问题/关联问题分离（ruled_out 区分，修复 D6）；
    - 错误码仅作假设信号（reason_codes 标注 error_code_signal，不等于根因）；
    - hypothesis_code_from_hint / build_evidence_from_case 归口。

全部为纯函数，无 IO、无外部模型、无 DB。
"""

from backend.vpn.models import DiagnosisRequest
from backend.vpn.rules import (
    AccountStatus,
    EvidenceDiagnosis,
    GatewayStatus,
    VpnEvidence,
    _account_status_from_text,
    build_evidence_from_case,
    evaluate_evidence,
    evaluate_evidence_handoff,
    hypothesis_code_from_hint,
    is_account_lockout_text,
    is_version_outdated,
    normalize_account_status,
    normalize_gateway_status,
)
from backend.vpn.service import VpnDiagnosisService

# ========== 信号归一化 ==========


def test_normalize_account_status():
    assert normalize_account_status({"status": "active", "found": True}) == AccountStatus.ACTIVE
    assert normalize_account_status({"status": "locked", "found": True}) == AccountStatus.LOCKED
    assert normalize_account_status({"status": "disabled"}) == AccountStatus.DISABLED
    assert normalize_account_status({"status": "expired"}) == AccountStatus.EXPIRED
    # 找不到 / found=false / 未知 -> UNKNOWN
    assert normalize_account_status({"found": False}) == AccountStatus.UNKNOWN
    assert normalize_account_status(None) == AccountStatus.UNKNOWN
    assert normalize_account_status({"status": "something-weird"}) == AccountStatus.UNKNOWN


def test_normalize_gateway_status():
    assert normalize_gateway_status({"status": "up", "found": True}) == GatewayStatus.UP
    assert normalize_gateway_status({"status": "down"}) == GatewayStatus.DOWN
    assert normalize_gateway_status({"status": "degraded"}) == GatewayStatus.DEGRADED
    assert normalize_gateway_status({"found": False}) == GatewayStatus.UNKNOWN
    assert normalize_gateway_status("") == GatewayStatus.UNKNOWN
    # 大小写 / 中文兜底
    assert normalize_gateway_status({"status": "Up"}) == GatewayStatus.UP


def test_is_version_outdated():
    assert is_version_outdated("2.9.0", "3.4.2") is True
    assert is_version_outdated("3.4.2", "3.4.2") is False
    assert is_version_outdated("3.5.0", "3.4.2") is False
    # 支持 "v" 前缀（mock 数据 v2.4.1）
    assert is_version_outdated("v2.4.1", "3.4.2") is True
    # 任一无法解析 -> False（不把「未知」当过期）
    assert is_version_outdated("unknown", "3.4.2") is False
    assert is_version_outdated("2.9.0", "") is False
    assert is_version_outdated("", "3.4.2") is False


# ========== 规则表 R0-R6 ==========


def test_r0_identity_missing():
    """身份缺失 -> no_evidence，必须人工，不给任何根因结论。"""
    d = evaluate_evidence(VpnEvidence(identity_ok=False))
    assert d.hypothesis == "no_evidence"
    assert d.must_handoff is True
    assert d.next_action == "escalate_incident"
    assert d.confidence < 0.5
    # 排除项明确，不给出根因假设
    assert "account_locked" in d.ruled_out
    assert "gateway_down" in d.ruled_out


def test_r1_multi_user_impact():
    """多用户同时失败 -> 事件升级，不走单用户建议。"""
    d = evaluate_evidence(VpnEvidence(multi_user_impact=True))
    assert d.hypothesis == "multi_user_impact"
    assert d.must_handoff is True
    assert d.next_action == "escalate_incident"
    assert "multi_user_impact" in d.reason_codes
    # 单用户/连接类假设均被排除（主问题/关联问题分离）
    assert "connection_failed" in d.ruled_out
    assert "client_version_outdated" in d.ruled_out


def test_r2_account_locked():
    """账号锁定 -> 必须人工(it.account)，不给 VPN 自动建议。"""
    d = evaluate_evidence(VpnEvidence(account_status=AccountStatus.LOCKED))
    assert d.hypothesis == "account_locked"
    assert d.must_handoff is True
    assert d.next_action == "escalate_incident"
    assert "account_locked" in d.reason_codes
    # 账号/VPN/网关主问题与关联问题分离：网关、版本、连接统统排除
    assert "gateway_down" in d.ruled_out
    assert "client_version_outdated" in d.ruled_out
    assert "connection_failed" in d.ruled_out


def test_r3_client_version_outdated_config_issue():
    """账号正常 + 网关 up + 客户端版本异常 -> 配置/客户端版本问题，自动建议步骤。"""
    d = evaluate_evidence(
        VpnEvidence(
            account_status=AccountStatus.ACTIVE,
            gateway_status=GatewayStatus.UP,
            client_version="2.9.0",
            required_version="3.4.2",
        )
    )
    assert d.hypothesis == "client_version_outdated"
    assert d.must_handoff is False
    assert d.next_action == "provide_steps"
    assert d.confidence >= 0.8
    assert "client_version_outdated" in d.reason_codes
    assert "config_issue" in d.reason_codes
    # 证据链完整（账号/网关/版本三项）
    assert len(d.evidence) >= 2


def test_r4_gateway_down():
    """网关 down/degraded -> 网关侧问题，必须人工。"""
    d = evaluate_evidence(
        VpnEvidence(account_status=AccountStatus.ACTIVE, gateway_status=GatewayStatus.DOWN)
    )
    assert d.hypothesis == "gateway_down"
    assert d.must_handoff is True
    assert d.next_action == "escalate_incident"
    assert "gateway_down" in d.reason_codes
    # degraded 视为网关侧问题（置信度略低）
    d2 = evaluate_evidence(
        VpnEvidence(account_status=AccountStatus.ACTIVE, gateway_status=GatewayStatus.DEGRADED)
    )
    assert d2.hypothesis == "gateway_down"
    assert d2.confidence < d.confidence


def test_r6_no_credible_evidence():
    """无任何可信证据 -> 不给根因结论，只能转人工（no_evidence）。"""
    d = evaluate_evidence(VpnEvidence())  # 全部信号未知
    assert d.hypothesis == "no_evidence"
    assert d.must_handoff is True
    assert d.next_action == "escalate_incident"
    assert d.confidence <= 0.3
    assert "no_evidence" in d.reason_codes
    # 不产出任何根因假设
    assert d.hypothesis == "no_evidence"


def test_r5_connection_failure_with_error_code_signal():
    """账号/网关正常 + 无版本过期 -> 连接类失败；错误码仅作假设信号，不等于根因。"""
    d = evaluate_evidence(
        VpnEvidence(
            account_status=AccountStatus.ACTIVE,
            gateway_status=GatewayStatus.UP,
            error_code="809",
            knowledge_hit=True,
        )
    )
    assert d.hypothesis == "connection_failed"
    assert d.must_handoff is False
    assert d.next_action == "provide_steps"
    # 错误码只作为假设信号标注，不能直接等于根因（根因仍是 connection_failed）
    assert any(c.startswith("error_code_signal=809") for c in d.reason_codes)
    assert "error_code" not in d.hypothesis


def test_r5_without_knowledge_escalates():
    """连接兜底但无知识命中 -> 不得给根因结论自动建议，转人工。"""
    d = evaluate_evidence(
        VpnEvidence(
            account_status=AccountStatus.ACTIVE,
            gateway_status=GatewayStatus.UP,
            error_code="809",
            knowledge_hit=False,
        )
    )
    assert d.must_handoff is True
    assert d.next_action == "escalate_incident"


# ========== 结构化字段完整 DAG ==========


def test_evidence_diagnosis_to_dict_shape():
    """结构化结论字段固定：hypothesis/confidence/evidence/ruled_out/next_action/reason_codes/must_handoff/requires_human。

    阶段四：目标结论 schema 的 6 字段（hypothesis/evidence/confidence/ruled_out/
    next_action/requires_human）必须全部存在；reason_codes/must_handoff 为兼容保留。
    """
    d = evaluate_evidence(VpnEvidence(account_status=AccountStatus.LOCKED))
    as_dict = d.to_dict()
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
    # 目标结论 schema 的 6 个关键字段必须齐全（阶段四对齐目标 JSON）。
    from backend.vpn.rules import CONCLUSION_FIELDS

    assert all(field in as_dict for field in CONCLUSION_FIELDS)
    assert isinstance(as_dict["evidence"], list)
    assert isinstance(as_dict["ruled_out"], list)
    assert isinstance(as_dict["reason_codes"], list)
    assert isinstance(as_dict["confidence"], float)
    assert isinstance(as_dict["requires_human"], bool)


# ========== evaluate_evidence_handoff 复用 models 四类兜底 ==========


def test_evaluate_evidence_handoff_integration():
    d, handoff = evaluate_evidence_handoff(
        VpnEvidence(
            account_status=AccountStatus.ACTIVE, gateway_status=GatewayStatus.UP, knowledge_hit=True
        )
    )
    assert isinstance(d, EvidenceDiagnosis)
    # 连接兜底：规则 must_handoff=False，handoff 也应为 False（证据充分+非群体+身份齐+置信度达标）
    assert handoff.must_handoff is False
    assert handoff.handoff_reasons == []


def test_evaluate_evidence_handoff_no_evidence_forces_handoff():
    d, handoff = evaluate_evidence_handoff(VpnEvidence())  # 无可信证据
    assert d.hypothesis == "no_evidence"
    assert handoff.must_handoff is True


# ========== 假设归口 ==========


def test_hypothesis_code_from_hint():
    assert (
        hypothesis_code_from_hint("客户端版本过旧，建议升级到 3.4.2") == "client_version_outdated"
    )
    assert hypothesis_code_from_hint("账号被锁定，需要重置密码") == "account_locked"
    assert hypothesis_code_from_hint("群体故障，整个部门都连不上") == "multi_user_impact"
    assert hypothesis_code_from_hint("网关 status=down") == "gateway_down"
    assert hypothesis_code_from_hint("无证据，需人工") == "no_evidence"
    # 已编码直接返回
    assert hypothesis_code_from_hint("connection_failed") == "connection_failed"
    # 无法匹配 -> 连接类兜底
    assert hypothesis_code_from_hint("一些无关文本") == "connection_failed"
    assert hypothesis_code_from_hint("") == "connection_failed"


def test_build_evidence_from_case():
    case = {
        "vpn_fault": "connection_failed",
        "fault_hypothesis": "客户端版本过旧，建议升级到 3.4.2",
        "provided_fields": {
            "client_version": "2.9.0",
            "error_code": "809",
            "multi_user_impacted": "否",
        },
        "expected_document_ids": ("vpn-001",),
        "asset_id": "asset-001",
    }
    ev = build_evidence_from_case(case, identity_ok=True)
    assert ev.fault == "connection_failed"
    assert ev.client_version == "2.9.0"
    assert ev.error_code == "809"
    assert ev.multi_user_impact is False
    assert ev.knowledge_hit is True  # 有 expected_document_ids
    assert ev.has_asset is True
    assert ev.identity_ok is True


def test_build_evidence_from_case_multi_user_flag():
    case = {
        "vpn_fault": "multi_user_impact",
        "fault_hypothesis": "群体故障",
        "provided_fields": {"multi_user_impacted": "是"},
    }
    ev = build_evidence_from_case(case)
    assert ev.multi_user_impact is True
    # 多用户影响命中规则 R1
    d = evaluate_evidence(ev)
    assert d.hypothesis == "multi_user_impact"
    assert d.must_handoff is True


# ========== 阶段三：D6 账号锁定-VPN 混淆的规则层深修复 ==========
# 说明：S6 样本 provided_fields 无 account_status、expected_document_ids=()。
# 修复前 build_evidence_from_case 得到 account_status=UNKNOWN -> R6 no_evidence，
# M3 hypothesis 回退 vpn_fault（connection_failed）。修复后必须能检测出账号锁定
# 主因 -> R2 account_locked + must_escalate，M3 hypothesis 不再回退 vpn_fault。


def test_account_status_from_text_detects_lockout_and_disable():
    # 显式锁定词
    assert _account_status_from_text("账号被锁定") == AccountStatus.LOCKED
    assert _account_status_from_text("VPN 连不上，说是账号被锁定了") == AccountStatus.LOCKED
    assert _account_status_from_text("账号锁定导致 VPN 连不上") == AccountStatus.LOCKED
    # 禁用（次要归 DISABLED）
    assert _account_status_from_text("账户已禁用") == AccountStatus.DISABLED
    assert _account_status_from_text("账号被禁用，无法登录") == AccountStatus.DISABLED
    # 弱信号（多次失败）且未落入认证排除框架 -> LOCKED
    assert _account_status_from_text("连续多次登录失败，怀疑账号被锁") == AccountStatus.LOCKED
    # 认证/密码失败（v1 伴生）-> 不误判为账号锁定
    assert _account_status_from_text("登录失败（密码错误）") == AccountStatus.UNKNOWN
    assert _account_status_from_text("VPN 认证失败，用户名或密码错误") == AccountStatus.UNKNOWN
    # 空/无关
    assert _account_status_from_text("") == AccountStatus.UNKNOWN
    assert _account_status_from_text("VPN 连不上，一直转圈") == AccountStatus.UNKNOWN


def test_is_account_lockout_text_helper():
    assert is_account_lockout_text("账号被锁定") is True
    assert is_account_lockout_text("账户已禁用") is True
    assert is_account_lockout_text("VPN 认证失败") is False
    assert is_account_lockout_text("VPN 连不上") is False


def test_s6_case_builds_account_locked_hypothesis():
    """S6 账号锁定样本即便无 account_status 字段、无预期文档，
    也必须经 build_evidence_from_case -> 检测账号锁定 -> R2 account_locked + must_escalate。"""
    s6_case = {
        "scenario": "account_lockout_vs_vpn",
        "text": "VPN 连不上，说是账号被锁定了",
        "vpn_fault": "connection_failed",
        "provided_fields": {"error_code": "无"},
        "expected_document_ids": (),
        "expected_boundary": "must_escalate",
        "is_negative": True,
        "fault_hypothesis": "账号被锁定被误表述为 VPN 连不上，应归类 it.account 并升级",
    }
    ev = build_evidence_from_case(s6_case, identity_ok=True)
    # 关键：账号状态必须被文本检测识别为 LOCKED，而非 UNKNOWN。
    assert ev.account_status == AccountStatus.LOCKED
    d = evaluate_evidence(ev)
    # M3 真实口径：hypothesis 为结构化 account_locked，不再回退 vpn_fault(connection_failed)。
    assert d.hypothesis == "account_locked"
    assert d.must_handoff is True
    assert d.next_action == "escalate_incident"
    assert "account_locked" in d.reason_codes
    # 主问题/关联问题分离：VPN 相关假设被排除。
    assert "connection_failed" in d.ruled_out
    assert "gateway_down" in d.ruled_out
    assert "client_version_outdated" in d.ruled_out


def test_s6_all_scenarios_map_to_account_locked():
    """对齐 vpn_eval_cases_v2 的 S6 场景（7 条）：每条都必须得 account_locked + 转人工。"""
    from backend.knowledge.vpn_eval_cases_v2 import SCENARIO_ACCOUNT_LOCK, V2_EVAL_CASES

    s6 = [c for c in V2_EVAL_CASES if c["scenario"] == SCENARIO_ACCOUNT_LOCK]
    assert len(s6) >= 6, "S6 至少 6 条"
    for case in s6:
        ev = build_evidence_from_case(case, identity_ok=True)
        d = evaluate_evidence(ev)
        assert d.hypothesis == "account_locked", case["text"]
        assert d.must_handoff is True, case["text"]
        assert d.next_action == "escalate_incident", case["text"]


def test_m3_hypothesis_does_not_fallback_for_account_lockout():
    """M3 真实口径：账号锁定样本的 evidence_hypothesis 必须是 account_locked，
    而非由 vpn_fault 回退得来的 connection_failed。"""
    s6_case = {
        "text": "账号锁定导致 VPN 连不上",
        "vpn_fault": "connection_failed",  # 回退口径下会得到 vpn_fault=connection_failed
        "provided_fields": {"error_code": "无"},
        "fault_hypothesis": "根因为账号锁定而非 VPN 故障，应归 it.account 并升级",
    }
    ev = build_evidence_from_case(s6_case, identity_ok=True)
    d = evaluate_evidence(ev)
    # 结构化假设（evidence_hypothesis）应是 account_locked，不是 vpn_fault。
    assert d.hypothesis == "account_locked"
    assert d.hypothesis != "connection_failed"
    # 预期假设编码（来自 fault_hypothesis 文本）也应归一为 account_locked。
    expected_code = hypothesis_code_from_hint(s6_case["fault_hypothesis"])
    assert expected_code == "account_locked"
    # M3 命中：expected_hypothesis_code == evidence_hypothesis。
    assert expected_code == d.hypothesis


# ========== 阶段三：service.apply_handoff 接入证据链（M3 真实口径落库） ==========


def _service_agent_result(**overrides) -> dict:
    base = {
        "tool_evidence": [
            {
                "tool_name": "get_vpn_account_status",
                "content": "账号 user-042 状态=locked",
                "found": True,
            },
            {
                "tool_name": "get_vpn_gateway_status",
                "content": "网关 gw-cn-north status=up",
                "found": True,
            },
        ],
        "tool_trace": [],
        "must_handoff": True,
        "evaluation": {"must_handoff": True, "handoff_reasons": ["no_evidence"], "confidence": 0.0},
        "command": {
            "command": "escalate_incident",
            "content": "账号被锁定，需人工处理",
            "payload": {},
            "reason_codes": [],
            "confidence": 0.9,
        },
    }
    base.update(overrides)
    return base


def _service_request() -> DiagnosisRequest:
    return DiagnosisRequest(
        ticket_id="t-1",
        requester_id="user-1",
        tenant_id="tenant-a",
        ticket_text="VPN 连不上，账号被锁定",
        fault="connection_failed",
        current_status="assigned",
        identity_ok=True,
    )


def test_apply_handoff_produces_evidence_chain_account_locked():
    """service.apply_handoff 应把账号锁定证据归一化为 VpnEvidence -> evidence_chain=account_locked。"""
    raw = _service_agent_result()
    result = VpnDiagnosisService.apply_handoff(raw, _service_request())
    assert "evidence_chain" in result
    ec = result["evidence_chain"]
    # M3 真实口径：结构化 hypothesis 为 account_locked（而非 command.content / vpn_fault）。
    assert ec["hypothesis"] == "account_locked"
    assert ec["must_handoff"] is True
    assert ec["next_action"] == "escalate_incident"
    assert "account_locked" in ec["reason_codes"]
    # ruled_out 排除 VPN 相关假设（主问题/关联问题分离）。
    assert "connection_failed" in ec["ruled_out"]
    assert "gateway_down" in ec["ruled_out"]


def test_apply_handoff_evidence_chain_gateway_down():
    """网关 down 证据 -> evidence_chain=gateway_down（账号正常，无版本过期）。"""
    raw = _service_agent_result(
        tool_evidence=[
            {
                "tool_name": "get_vpn_account_status",
                "content": "账号 user-042 状态=active",
                "found": True,
            },
            {
                "tool_name": "get_vpn_gateway_status",
                "content": "网关 gw-north status=down",
                "found": True,
            },
        ]
    )
    result = VpnDiagnosisService.apply_handoff(raw, _service_request())
    ec = result["evidence_chain"]
    assert ec["hypothesis"] == "gateway_down"
    assert ec["must_handoff"] is True


def test_apply_handoff_evidence_chain_client_version_ok():
    """无锁定/无网关故障/有知识/有版本 -> 连接类兜底（provide_steps 或 escalate）。"""
    raw = _service_agent_result(
        tool_evidence=[
            {
                "tool_name": "get_vpn_account_status",
                "content": "账号 user-042 状态=active",
                "found": True,
            },
            {
                "tool_name": "get_vpn_gateway_status",
                "content": "网关 gw-north status=up",
                "found": True,
            },
            {"tool_name": "search_vpn_knowledge", "content": "知识库命中 vpn-001", "found": True},
        ],
        command={
            "command": "provide_steps",
            "content": "请检查客户端版本",
            "payload": {},
            "reason_codes": [],
            "confidence": 0.87,
        },
    )
    result = VpnDiagnosisService.apply_handoff(raw, _service_request())
    ec = result["evidence_chain"]
    assert ec["hypothesis"] == "connection_failed"
    assert ec["must_handoff"] is False
    assert ec["next_action"] == "provide_steps"


def test_apply_handoff_evidence_chain_empty_no_evidence():
    """无可信证据 -> evidence_chain=no_evidence，强制必须人工。"""
    raw = _service_agent_result(tool_evidence=[], command=None)
    result = VpnDiagnosisService.apply_handoff(raw, _service_request())
    ec = result["evidence_chain"]
    assert ec["hypothesis"] == "no_evidence"
    assert ec["must_handoff"] is True
    assert ec["next_action"] == "escalate_incident"
