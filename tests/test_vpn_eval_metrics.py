"""VPN 诊断链路 9 项关键指标模块单元测试（backend/vpn_eval_metrics.py）。

覆盖（构造假样本，验证各指标计算正确）：
    - M1 VPN 分类 Top1（命中/未命中/负向排除）
    - M2 字段补全（检测正确率 + 完整率）
    - M3 故障假设命中率（回退 vpn_fault / 显式假设 / 负向排除）
    - M4 人工升级准确率（TP/FP/FN/TN + recall/precision/accuracy，含命令与 must_handoff 口径）
    - M5 客户平均排障轮次（执行步骤 + 诊断轮数 proxy + 无数据）
    - M6 引用支撑率（db/static 两态）
    - M7 高风险误放行率（误放行 vs 正确升级）
    - M8 工具调用失败率（denied/timeout/error 计失败，completed 计成功）
    - M9 P95 延迟 + 单工单成本（rated / unrated / 不抛异常）
    - compute_metrics_report 汇总、to_record 规范化的桥接正确性

全部为纯函数，无 IO、无外部模型。
"""

from backend.vpn_eval_metrics import (
    VpnEvalRecord,
    compute_acl_rejection,
    compute_classification,
    compute_cost,
    compute_customer_step_completion,
    compute_customer_steps,
    compute_escalation,
    compute_evidence_sufficiency,
    compute_fault_hypothesis_accuracy,
    compute_field_completion,
    compute_high_risk_misdirect,
    compute_hypothesis_hit,
    compute_latency,
    compute_manual_takeover,
    compute_metrics_report,
    compute_rediagnosis_success,
    compute_reference_support,
    compute_tool_failure,
    compute_wrong_escalation,
    records_from_diagnosis_snapshots,
    to_record,
    to_record_from_snapshot,
)

# ========== M1 VPN 分类 Top1 ==========


def test_classification_hit_miss_and_negative_excluded():
    records = [
        VpnEvalRecord(is_negative=False, expected_fault="connection_failed", predicted_fault="connection_failed"),  # hit
        VpnEvalRecord(is_negative=False, expected_fault="auth_failed", predicted_fault="connection_failed"),  # miss
        VpnEvalRecord(is_negative=True, expected_fault="connection_failed", predicted_fault="connection_failed"),  # excluded
    ]
    out = compute_classification(records)
    assert out["sample_count"] == 2
    assert out["accuracy"] == 0.5


def test_classification_empty_returns_zero_rate():
    out = compute_classification([])
    assert out["sample_count"] == 0
    assert out["accuracy"] == 1.0  # 无真实样本时视为无失败


# ========== M2 字段补全 ==========


def test_field_completion_detection_and_complete_rate():
    records = [
        VpnEvalRecord(is_negative=False, expected_missing=("device",), actual_missing=("device",)),  # 检测正确
        VpnEvalRecord(is_negative=False, expected_missing=(), actual_missing=("network",)),  # 检测错误
        VpnEvalRecord(is_negative=False, expected_missing=(), actual_missing=()),  # 检测正确 + 完整
        VpnEvalRecord(is_negative=True, expected_missing=(), actual_missing=()),  # 负向，检测正确
    ]
    out = compute_field_completion(records)
    assert out["detection_rate"] == 0.75  # 3/4
    assert out["complete_rate"] == 0.3333  # 1/3（只统计真实 VPN 样本）
    assert out["sample_count"] == 4


# ========== M3 故障假设命中率 ==========


def test_hypothesis_hit_fallback_and_negative_excluded():
    records = [
        VpnEvalRecord(is_negative=False, expected_fault="connection_failed", predicted_fault="connection_failed"),  # 回退命中
        VpnEvalRecord(is_negative=False, expected_fault="auth_failed", predicted_fault="connection_failed"),  # 回退未命中
        VpnEvalRecord(  # 显式假设命中
            is_negative=False,
            expected_fault="connection_failed",
            predicted_fault="connection_failed",
            expected_hypothesis="gateway_down",
            predicted_hypothesis="gateway_down",
        ),
        VpnEvalRecord(is_negative=True, expected_fault="connection_failed", predicted_fault="connection_failed"),  # 负向排除
    ]
    out = compute_hypothesis_hit(records)
    assert out["sample_count"] == 3
    assert out["hit_rate"] == 0.6667
    assert "回退 vpn_fault" in out["basis"]


def test_hypothesis_hit_no_applicable_returns_none():
    out = compute_hypothesis_hit([VpnEvalRecord(is_negative=True)])
    assert out["sample_count"] == 0
    assert out["hit_rate"] is None


# ========== M4 人工升级准确率 ==========


def test_escalation_tp_fp_fn_tn_all_paths():
    records = [
        VpnEvalRecord(expected_boundary="must_escalate", predicted_boundary="must_escalate"),  # TP
        VpnEvalRecord(expected_boundary="must_escalate", predicted_boundary="auto_suggest"),  # FN
        VpnEvalRecord(expected_boundary="auto_suggest", predicted_boundary="must_escalate"),  # FP
        VpnEvalRecord(expected_boundary="auto_suggest", predicted_boundary="auto_suggest"),  # TN
        VpnEvalRecord(  # TP 经命令口径
            expected_boundary="must_escalate",
            predicted_boundary="must_escalate",
            produced_command="escalate_incident",
        ),
        VpnEvalRecord(  # TP 经 must_handoff 且无命令
            expected_boundary="must_escalate",
            produced_command=None,
            diagnosis_must_handoff=True,
        ),
    ]
    out = compute_escalation(records)
    assert out["tp"] == 3
    assert out["fn"] == 1
    assert out["fp"] == 1
    assert out["tn"] == 1
    assert out["recall"] == 0.75  # 3/(3+1)
    assert out["precision"] == 0.75  # 3/(3+1)
    assert out["accuracy"] == 0.6667  # (3+1)/6
    assert out["sample_count"] == 6


def test_escalation_no_samples_returns_none_rates():
    out = compute_escalation([])
    assert out["tp"] == 0
    assert out["recall"] is None
    assert out["accuracy"] is None


# ========== M5 客户平均排障轮次 ==========


def test_customer_steps_avg_with_proxy_fallback():
    records = [
        VpnEvalRecord(customer_step_count=3),
        VpnEvalRecord(customer_step_count=5),
        VpnEvalRecord(diagnostic_rounds=2),  # proxy
        VpnEvalRecord(),  # 无数据，不计入
    ]
    out = compute_customer_steps(records)
    assert out["sample_count"] == 3
    assert out["avg_steps"] == 3.3333  # (3+5+2)/3


def test_customer_steps_no_data_returns_none():
    out = compute_customer_steps([VpnEvalRecord(), VpnEvalRecord()])
    assert out["sample_count"] == 0
    assert out["avg_steps"] is None


# ========== M6 引用支撑率 ==========


def test_reference_support_db_and_static_modes():
    records = [
        VpnEvalRecord(expected_boundary="auto_suggest", reference_supported=True),  # 命中
        VpnEvalRecord(expected_boundary="auto_suggest", reference_supported=False),  # 未命中
        VpnEvalRecord(expected_boundary="must_escalate", reference_supported=True),  # 非 auto_suggest 排除
        VpnEvalRecord(expected_boundary="auto_suggest", reference_supported=None),  # 无判定，排除
    ]
    db_out = compute_reference_support(records, mode="db")
    assert db_out["support_rate"] == 0.5
    assert db_out["denominator"] == 2
    assert db_out["numerator"] == 1
    assert db_out["mode"] == "db"

    static_out = compute_reference_support(records, mode="static")
    assert static_out["support_rate"] is None
    assert static_out["denominator"] is None


# ========== M7 高风险误放行率 ==========


def test_high_risk_misdirect_rate():
    records = [
        VpnEvalRecord(is_negative=False, is_high_risk=True, predicted_boundary="auto_suggest"),  # 非负向，不计入分母
        VpnEvalRecord(is_negative=True, predicted_boundary="auto_suggest"),  # 负向 + auto_suggest -> 误放行
        VpnEvalRecord(is_negative=True, predicted_boundary="must_escalate"),  # 负向 + 正确升级
    ]
    out = compute_high_risk_misdirect(records)
    assert out["negative_sample_count"] == 2
    assert out["misdirect_count"] == 1
    assert out["misdirect_rate"] == 0.5
    assert len(out["samples"]) == 1


def test_high_risk_misdirect_ideal_zero():
    out = compute_high_risk_misdirect(
        [VpnEvalRecord(is_negative=True, predicted_boundary="must_escalate")]
    )
    assert out["misdirect_count"] == 0
    assert out["misdirect_rate"] == 0.0
    assert out["negative_sample_count"] == 1
    assert out["samples"] == []


# ========== M8 工具调用失败率 ==========


def test_tool_failure_counts_failed_statuses_only():
    records = [
        VpnEvalRecord(tool_call_statuses=("completed", "completed")),  # 0 失败
        VpnEvalRecord(tool_call_statuses=("denied", "timeout")),  # 2 失败（越权/超时）
        VpnEvalRecord(tool_call_statuses=("completed", "error")),  # 1 失败
        VpnEvalRecord(),  # 无调用
    ]
    out = compute_tool_failure(records)
    assert out["total_count"] == 6
    assert out["failed_count"] == 3
    assert out["failure_rate"] == 0.5


def test_tool_failure_no_calls_returns_none():
    out = compute_tool_failure([VpnEvalRecord(), VpnEvalRecord()])
    assert out["total_count"] == 0
    assert out["failure_rate"] is None


# ========== M9 P95 延迟 ==========


def test_latency_p50_p95():
    records = [
        VpnEvalRecord(latency_ms=120),
        VpnEvalRecord(latency_ms=80),
        VpnEvalRecord(latency_ms=200),
        VpnEvalRecord(latency_ms=None),  # 排除
    ]
    out = compute_latency(records)
    assert out["sample_count"] == 3
    assert out["p50"] == 120.0
    assert out["p95"] == 200.0


def test_latency_empty_returns_zero():
    out = compute_latency([])
    assert out["sample_count"] == 0
    assert out["p50"] == 0.0
    assert out["p95"] == 0.0


# ========== M9 单工单成本 ==========


def test_cost_rated():
    records = [
        VpnEvalRecord(input_tokens=1000, output_tokens=500),
        VpnEvalRecord(input_tokens=1000, output_tokens=500),
    ]
    out = compute_cost(records, input_per_1k=1.0, output_per_1k=2.0)
    assert out["total_usd"] == 4.0  # (2000/1000)*1.0 + (1000/1000)*2.0
    assert out["ticket_count"] == 2
    assert out["per_ticket_usd"] == 2.0
    assert out["rated"] is True


def test_cost_unrated_zero_price_does_not_throw():
    records = [VpnEvalRecord(input_tokens=1000, output_tokens=200)]
    out = compute_cost(records, input_per_1k=0.0, output_per_1k=0.0)
    assert out["total_usd"] == 0.0
    assert out["per_ticket_usd"] is None  # unrated
    assert out["rated"] is False


def test_cost_empty_records_does_not_throw():
    out = compute_cost([])
    assert out["ticket_count"] == 0
    assert out["total_usd"] == 0.0


# ========== 汇总集成 ==========


def test_compute_metrics_report_integration_shape():
    records = [
        VpnEvalRecord(
            is_negative=False,
            expected_fault="connection_failed",
            predicted_fault="connection_failed",
            expected_boundary="auto_suggest",
            predicted_boundary="auto_suggest",
            reference_supported=True,
            latency_ms=10.0,
        ),
        VpnEvalRecord(
            is_negative=False,
            expected_fault="auth_failed",
            predicted_fault="auth_failed",
            expected_boundary="must_escalate",
            predicted_boundary="must_escalate",
            is_high_risk=True,
            tool_call_statuses=("denied",),
        ),
        VpnEvalRecord(
            is_negative=True,
            expected_fault="connection_failed",
            predicted_fault="auth_failed",
            expected_boundary="must_escalate",
            predicted_boundary="auto_suggest",
            is_high_risk=True,
            tool_call_statuses=("completed", "error"),
        ),
    ]
    report = compute_metrics_report(records, mode="db", input_per_1k=0.5, output_per_1k=1.5)
    # 10 项指标小节都存在（9 项关键指标 + D3 新增 ACL 越权拒绝率）
    for key in (
        "vpn_fault_classification",
        "field_completion",
        "fault_hypothesis_hit",
        "escalation_accuracy",
        "customer_steps",
        "reference_support",
        "high_risk_misdirect",
        "acl_rejection",
        "tool_failure",
        "latency",
        "cost",
    ):
        assert key in report
    # 抽查几项：分类（2 真实样本全命中；负向样本被排除）
    assert report["vpn_fault_classification"]["sample_count"] == 2
    assert report["vpn_fault_classification"]["accuracy"] == 1.0
    # 升级：只有第 2 条需升级且升级(TP)；第 3 条需升级但未升级(FN)
    assert report["escalation_accuracy"]["tp"] == 1
    assert report["escalation_accuracy"]["fn"] == 1
    # 工具失败：denied + error = 2 失败 / 3 调用
    assert report["tool_failure"]["failed_count"] == 2
    assert report["tool_failure"]["total_count"] == 3
    assert report["tool_failure"]["failure_rate"] == 0.6667
    # 语义高风险误放行：负向样本 1 个（第 3 条）被误放行
    assert report["high_risk_misdirect"]["misdirect_count"] == 1
    assert report["high_risk_misdirect"]["negative_sample_count"] == 1
    # ACL 越权拒绝率：本批无 ACL 样本 -> None
    assert report["acl_rejection"]["acl_sample_count"] == 0
    assert report["acl_rejection"]["rejection_rate"] is None


# ========== D3 ACL 越权拒绝率 ==========


def test_acl_rejection_rate():
    records = [
        VpnEvalRecord(
            scenario="acl_out_of_scope", is_negative=True, predicted_boundary="must_escalate"
        ),
        VpnEvalRecord(
            scenario="acl_out_of_scope", is_negative=True, predicted_boundary="must_escalate"
        ),
        VpnEvalRecord(
            scenario="acl_out_of_scope", is_negative=True, predicted_boundary="auto_suggest"
        ),
        VpnEvalRecord(scenario="no_knowledge_answer", is_negative=True, predicted_boundary="must_escalate"),
    ]
    out = compute_acl_rejection(records)
    assert out["acl_sample_count"] == 3
    assert out["rejected_count"] == 2
    assert out["rejection_rate"] == 0.6667
    assert len(out["samples"]) == 1  # 第 3 条未拒绝
    assert out["samples"][0]["predicted_boundary"] == "auto_suggest"


def test_acl_rejection_ideal_all_rejected():
    out = compute_acl_rejection(
        [VpnEvalRecord(scenario="acl_out_of_scope", predicted_boundary="must_escalate")]
    )
    assert out["rejection_rate"] == 1.0
    assert out["samples"] == []


def test_acl_rejection_no_acl_samples_returns_none():
    out = compute_acl_rejection([VpnEvalRecord(scenario="high_risk_request", predicted_boundary="must_escalate")])
    assert out["acl_sample_count"] == 0
    assert out["rejection_rate"] is None


# ========== 与 run_vpn_eval 的桥接 ==========


def test_to_record_maps_case_result():
    case_result = {
        "index": 7,
        "scenario": "acl_out_of_scope",
        "text": "VPN 跨租户访问其他部门的数据",
        "is_negative": True,
        "vpn_fault": "connection_failed",
        "predicted_fault": "connection_failed",
        "expected_missing": ("device",),
        "actual_missing": ("device",),
        "expected_boundary": "must_escalate",
        "predicted_boundary": "must_escalate",
        "expected_document_ids": ["vpn-001"],
        "retrieved_document_ids": [],
        "reference_supported": None,
        "has_evidence": True,
        "has_sensitive_risk": True,
        "has_high_impact": False,
        "misdirect": False,
        "latency_ms": 15.5,
        "error_code": "无",
        "client_version": "3.4.2",
        "network_type": "办公网",
        "fault_hypothesis": "跨租户越权读取，拒绝并升级",
        "acls": ["demo:vpn:read"],
        "risk_level": "high",
        "escalation_expected": True,
        "departments": ["finance"],
        "internal": False,
        "resource": "其他租户 VPN 数据",
        # 阶段三：结构化证据链假设字段
        "expected_hypothesis_code": "client_version_outdated",
        "evidence_hypothesis": "connection_failed",
        "evidence_found": True,
    }
    rec = to_record(case_result)
    assert rec.index == 7
    assert rec.scenario == "acl_out_of_scope"
    assert rec.text == "VPN 跨租户访问其他部门的数据"
    assert rec.is_negative is True
    assert rec.expected_fault == "connection_failed"
    assert rec.expected_missing == ("device",)
    assert rec.actual_missing == ("device",)
    assert rec.reference_supported is None
    assert rec.is_high_risk is True  # has_sensitive_risk=>True
    assert rec.latency_ms == 15.5
    # D3：v2 新增字段透传
    assert rec.error_code == "无"
    assert rec.client_version == "3.4.2"
    assert rec.network_type == "办公网"
    assert rec.fault_hypothesis == "跨租户越权读取，拒绝并升级"
    assert rec.acls == ("demo:vpn:read",)
    assert rec.risk_level == "high"
    assert rec.escalation_expected is True
    assert rec.departments == ("finance",)
    assert rec.internal is False
    assert rec.resource == "其他租户 VPN 数据"
    assert rec.tool_call_statuses == ()  # 桥接默认空
    # 阶段三：结构化证据链假设字段透传
    assert rec.expected_hypothesis_code == "client_version_outdated"
    assert rec.evidence_hypothesis == "connection_failed"
    assert rec.evidence_found is True
    assert rec.customer_steps_completed is None
    assert rec.is_rediagnosis is False


# ========== 阶段三：M3 真实口径（结构化证据链假设命中） ==========


def test_fault_hypothesis_accuracy_structured_hit_and_miss():
    records = [
        VpnEvalRecord(
            is_negative=False,
            expected_hypothesis_code="client_version_outdated",
            evidence_hypothesis="client_version_outdated",
        ),  # hit
        VpnEvalRecord(
            is_negative=False,
            expected_hypothesis_code="gateway_down",
            evidence_hypothesis="connection_failed",
        ),  # miss
        VpnEvalRecord(
            is_negative=True,
            expected_hypothesis_code="account_locked",
            evidence_hypothesis="account_locked",
        ),  # 负向排除
        VpnEvalRecord(is_negative=False),  # 无结构化编码，不计入
    ]
    out = compute_fault_hypothesis_accuracy(records)
    assert out["sample_count"] == 2
    assert out["hit_rate"] == 0.5
    assert out["structural_only"] is True
    assert "不回退 vpn_fault" in out["basis"]


def test_fault_hypothesis_accuracy_no_structured_returns_none():
    out = compute_fault_hypothesis_accuracy([VpnEvalRecord(is_negative=False)])
    assert out["sample_count"] == 0
    assert out["hit_rate"] is None


# ========== 阶段三：证据充分率 ==========


def test_evidence_sufficiency_rate():
    records = [
        VpnEvalRecord(is_negative=False, evidence_found=True),
        VpnEvalRecord(is_negative=False, evidence_found=True),
        VpnEvalRecord(is_negative=False, evidence_found=False),  # no_evidence
        VpnEvalRecord(is_negative=True, evidence_found=False),  # 负向排除
    ]
    out = compute_evidence_sufficiency(records)
    assert out["sample_count"] == 3
    assert out["sufficient_count"] == 2
    assert out["sufficiency_rate"] == 0.6667


def test_evidence_sufficiency_empty_returns_none():
    out = compute_evidence_sufficiency([])
    assert out["sample_count"] == 0
    assert out["sufficiency_rate"] is None


# ========== 阶段三：错误升级率 ==========


def test_wrong_escalation_rate_fp_only():
    records = [
        VpnEvalRecord(expected_boundary="auto_suggest", predicted_boundary="must_escalate"),  # wrong
        VpnEvalRecord(expected_boundary="auto_suggest", predicted_boundary="auto_suggest"),  # ok
        VpnEvalRecord(expected_boundary="must_escalate", predicted_boundary="must_escalate"),  # 需升级，不计入分母
    ]
    out = compute_wrong_escalation(records)
    assert out["sample_count"] == 2  # 仅无需升级的样本
    assert out["wrong_count"] == 1
    assert out["wrong_escalation_rate"] == 0.5


def test_wrong_escalation_ideal_zero():
    out = compute_wrong_escalation(
        [
            VpnEvalRecord(expected_boundary="auto_suggest", predicted_boundary="auto_suggest"),
            VpnEvalRecord(expected_boundary="must_ask", predicted_boundary="must_ask"),
        ]
    )
    assert out["wrong_count"] == 0
    assert out["wrong_escalation_rate"] == 0.0


# ========== 阶段三：客户步骤完成率 ==========


def test_customer_step_completion_rate():
    records = [
        VpnEvalRecord(is_negative=False, customer_steps_completed=True),
        VpnEvalRecord(is_negative=False, customer_steps_completed=False),
        VpnEvalRecord(is_negative=False, customer_steps_completed=None),  # 无闭环数据，不计入
    ]
    out = compute_customer_step_completion(records)
    assert out["sample_count"] == 2
    assert out["completed_count"] == 1
    assert out["completion_rate"] == 0.5


def test_customer_step_completion_static_empty():
    out = compute_customer_step_completion([VpnEvalRecord(is_negative=False)])
    assert out["sample_count"] == 0
    assert out["completion_rate"] is None


# ========== 阶段三：再次诊断成功率 ==========


def test_rediagnosis_success_rate():
    records = [
        VpnEvalRecord(is_rediagnosis=True, rediagnosis_success=True),
        VpnEvalRecord(is_rediagnosis=True, rediagnosis_success=False),
        VpnEvalRecord(is_rediagnosis=False, rediagnosis_success=True),  # 非再次诊断，不计入
    ]
    out = compute_rediagnosis_success(records)
    assert out["sample_count"] == 2
    assert out["success_count"] == 1
    assert out["success_rate"] == 0.5


def test_rediagnosis_success_static_empty():
    out = compute_rediagnosis_success([VpnEvalRecord(is_negative=False)])
    assert out["sample_count"] == 0
    assert out["success_rate"] is None


# ========== 阶段三：平均人工接管率 ==========


def test_manual_takeover_rate_field_priority():
    records = [
        VpnEvalRecord(predicted_boundary="must_escalate", manual_takeover=True),
        VpnEvalRecord(predicted_boundary="auto_suggest", manual_takeover=False),  # 字段优先
        VpnEvalRecord(predicted_boundary="auto_suggest", produced_command="auto_suggest"),
        VpnEvalRecord(predicted_boundary="auto_suggest", produced_command="escalate_incident"),
    ]
    out = compute_manual_takeover(records)
    # 第 1 条字段 True，第 2 条字段 False，第 3 条推断 False，第 4 条命令 escalate -> True
    assert out["sample_count"] == 4
    assert out["takeover_count"] == 2
    assert out["takeover_rate"] == 0.5


def test_manual_takeover_inferred_from_boundary():
    out = compute_manual_takeover(
        [VpnEvalRecord(predicted_boundary="must_escalate"), VpnEvalRecord(predicted_boundary="auto_suggest")]
    )
    assert out["takeover_count"] == 1
    assert out["takeover_rate"] == 0.5


# ========== 阶段三：汇总含新指标小节 ==========


def test_compute_metrics_report_includes_phase3_keys():
    records = [
        VpnEvalRecord(
            is_negative=False,
            expected_hypothesis_code="client_version_outdated",
            evidence_hypothesis="client_version_outdated",
            evidence_found=True,
            expected_boundary="auto_suggest",
            predicted_boundary="auto_suggest",
        ),
        VpnEvalRecord(
            is_negative=False,
            expected_hypothesis_code="gateway_down",
            evidence_hypothesis="connection_failed",
            evidence_found=True,
            expected_boundary="must_escalate",
            predicted_boundary="must_escalate",
        ),
    ]
    report = compute_metrics_report(records)
    for key in (
        "fault_hypothesis_accuracy",
        "evidence_sufficiency",
        "wrong_escalation",
        "customer_step_completion",
        "rediagnosis_success",
        "manual_takeover",
    ):
        assert key in report
    assert report["fault_hypothesis_accuracy"]["sample_count"] == 2
    assert report["fault_hypothesis_accuracy"]["hit_rate"] == 0.5
    assert report["evidence_sufficiency"]["sufficient_count"] == 2
    assert report["evidence_sufficiency"]["sufficiency_rate"] == 1.0


# ========== 阶段三：与 t2 诊断闭环快照的取数桥接 ==========


def test_to_record_from_snapshot_customer_completion_and_rediagnosis():
    """两次诊断（再次诊断）+ 客户步骤 + 回填结果 -> 各字段正确取值。"""
    snapshot = {
        "runs": [
            {"run_id": "r1", "hypothesis": "gateway_down", "confidence": 0.9, "next_action": "provide_steps", "status": "completed", "evidence": [{"tool_name": "x"}]},
            {"run_id": "r2", "hypothesis": "connection_failed", "confidence": 0.85, "next_action": "escalate_incident", "status": "handed_off", "reason_codes": ["handoff:no_evidence"]},
        ],
        "actions": [{"action_id": "a1"}],
        "results": [{"action_id": "a1", "result": "已重连"}],
    }
    rec = to_record_from_snapshot(snapshot, index=3, expected_hypothesis_code="connection_failed")
    assert rec.index == 3
    assert rec.is_rediagnosis is True
    assert rec.rediagnosis_success is True  # 最近一次 run handed_off
    assert rec.customer_steps_completed is True  # 有步骤且有回填
    assert rec.manual_takeover is True  # escalate_incident / handed_off
    assert rec.evidence_hypothesis == "connection_failed"
    assert rec.hypothesis_confidence == 0.85
    assert rec.evidence_found is True
    assert rec.produced_command == "escalate_incident"
    assert rec.diagnosis_must_handoff is True


def test_to_record_from_snapshot_no_rediagnosis():
    """单次诊断 + 客户步骤但未回填 -> 不适用字段为 None，未转人工。"""
    snapshot = {
        "runs": [
            {"run_id": "r1", "hypothesis": "client_version_outdated", "confidence": 0.85, "next_action": "provide_steps", "status": "completed", "evidence": [{"tool_name": "x"}]}
        ],
        "actions": [{"action_id": "a1"}],
        "results": [],
    }
    rec = to_record_from_snapshot(snapshot, expected_hypothesis_code="client_version_outdated")
    assert rec.is_rediagnosis is False
    assert rec.rediagnosis_success is None
    assert rec.customer_steps_completed is False  # 有步骤但未回填 -> 未完成
    assert rec.manual_takeover is False  # provide_steps 非转人工
    assert rec.evidence_hypothesis == "client_version_outdated"


def test_to_record_from_snapshot_empty_returns_none_fields():
    """空快照（无 run）-> 相关字段回退 None/False，不抛异常。"""
    rec = to_record_from_snapshot({})
    assert rec.is_rediagnosis is False
    assert rec.rediagnosis_success is None
    assert rec.customer_steps_completed is None
    assert rec.manual_takeover is None
    assert rec.evidence_hypothesis is None
    assert rec.evidence_found is False


def test_to_record_from_snapshot_failed_rediagnosis():
    """再次诊断但最近一次 run failed -> 再诊断失败。"""
    snapshot = {
        "runs": [
            {"run_id": "r1", "next_action": "provide_steps", "status": "completed"},
            {"run_id": "r2", "next_action": "ask_customer", "status": "failed"},
        ],
        "actions": [{"action_id": "a1"}],
        "results": [{"action_id": "a1", "result": "x"}],
    }
    rec = to_record_from_snapshot(snapshot)
    assert rec.is_rediagnosis is True
    assert rec.rediagnosis_success is False
    assert rec.manual_takeover is True  # failed 状态视为转人工
    assert rec.customer_steps_completed is True


def test_records_from_diagnosis_snapshots_batch():
    snapshots = [
        {
            "runs": [{"run_id": "r1", "hypothesis": "gateway_down", "confidence": 0.9, "next_action": "escalate_incident", "status": "handed_off", "evidence": [{}]}],
            "actions": [{"action_id": "a1"}],
            "results": [{"action_id": "a1", "result": "x"}],
        },
        {
            "runs": [{"run_id": "r1", "hypothesis": "client_version_outdated", "confidence": 0.85, "next_action": "provide_steps", "status": "completed", "evidence": [{}]}],
            "actions": [{"action_id": "a1"}],
            "results": [],
        },
    ]
    recs = records_from_diagnosis_snapshots(
        snapshots, expected_hypothesis_codes=["gateway_down", "client_version_outdated"]
    )
    assert len(recs) == 2
    assert recs[0].manual_takeover is True
    assert recs[0].evidence_hypothesis == "gateway_down"
    assert recs[1].manual_takeover is False
    assert recs[1].customer_steps_completed is False
    assert recs[1].is_rediagnosis is False
