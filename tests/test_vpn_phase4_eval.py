"""Phase 4 —— VPN 诊断深度冻结样本评测 harness 单元测试。

模块归属：tests/test_vpn_phase4_eval.py（本任务 t3 新增）。覆盖：
    - load_frozen_samples 读取 backend/vpn/eval/frozen_samples.json（>=20 条、gold 标签齐全）；
    - build_evidence_from_sample / predict_sample 归一化 + 确定性诊断；
    - run_frozen_metrics 全套指标（分类>=0.90 / 假设识别>=0.95 / 证据引用=1.00 /
      高风险误自动=0 / 多人影响误判=0 / 无证据自动回复=0）与门禁断言；
    - 单条指标函数的越界检测（门禁失败被正确识别）。

全部纯函数、无 IO、无真实模型/DB/VPN 凭证；不跑 CLI。
"""


from backend.vpn.eval_metrics import (
    FrozenSample,
    build_evidence_from_sample,
    high_risk_false_auto,
    load_frozen_samples,
    multi_user_misclassified,
    no_evidence_auto_reply,
    predict_sample,
    run_frozen_metrics,
)

_DEFAULT_PATH = "backend/vpn/eval/frozen_samples.json"


def test_frozen_samples_load_and_schema():
    samples = load_frozen_samples(_DEFAULT_PATH)
    assert len(samples) >= 20, len(samples)
    for s in samples:
        assert s.id
        assert s.text
        assert s.fault
        assert s.hypothesis
        # evidence_quote 必须是从文本中可引用的子串（否则证据引用完整率无意义）。
        assert s.evidence_quote in s.text, f"{s.id}: evidence_quote 不是文本子串"
        assert isinstance(s.requires_human, bool)


def test_predict_hypothesis_matches_gold():
    """gold 假设与确定性规则预测一致（本冻结集自洽性验证）。"""
    samples = load_frozen_samples(_DEFAULT_PATH)
    for s in samples:
        pred = predict_sample(s)
        assert pred.hypothesis == s.hypothesis, f"{s.id}: {pred.hypothesis} != {s.hypothesis}"


def test_full_metrics_gates_all_pass():
    samples = load_frozen_samples(_DEFAULT_PATH)
    report = run_frozen_metrics(samples)
    assert report["sample_count"] == len(samples)
    m = report["metrics"]
    assert m["classification_accuracy"]["value"] >= 0.90
    assert m["hypothesis_recognition"]["value"] >= 0.95
    assert m["evidence_citation_completeness"]["value"] >= 1.00
    assert m["high_risk_false_auto"]["value"] == 0
    assert m["multi_user_misclassified"]["value"] == 0
    assert m["no_evidence_auto_reply"]["value"] == 0
    assert report["all_gates_pass"] is True
    assert all(g["pass"] for g in report["gates"].values())


def test_metric_functions_detect_violations():
    """单条指标函数能在越界时正确计数（门禁失败检测）。"""
    rows = [
        {
            "id": "x1",
            "text": "t",
            "gold_fault": "connection_failed",
            "gold_hypothesis": "connection_failed",
            "gold_requires_human": True,  # 高风险但预测放行
            "evidence_quote": "q",
            "pred_hypothesis": "connection_failed",
            "pred_class": "connection_failed",
            "pred_evidence": ["q"],
            "pred_requires_human": False,  # 允许自动 -> 高风险误自动
            "pred_next_action": "escalate_incident",
        },
        {
            "id": "x2",
            "text": "t",
            "gold_fault": "multi_user_impact",
            "gold_hypothesis": "multi_user_impact",
            "gold_requires_human": True,
            "evidence_quote": "q",
            "pred_hypothesis": "connection_failed",  # 误判为单用户
            "pred_class": "connection_failed",
            "pred_evidence": ["q"],
            "pred_requires_human": False,
            "pred_next_action": "escalate_incident",
        },
        {
            "id": "x3",
            "text": "t",
            "gold_fault": "connection_failed",
            "gold_hypothesis": "no_evidence",
            "gold_requires_human": True,
            "evidence_quote": "q",
            "pred_hypothesis": "no_evidence",
            "pred_class": "no_evidence",
            "pred_evidence": [],
            "pred_requires_human": False,  # 无证据却允许自动 -> 无证据自动回复
            "pred_next_action": "escalate_incident",
        },
    ]
    # 三条样本 gold 均要求人工但预测均放行 -> 高风险误自动=3。
    assert high_risk_false_auto(rows) == (3, 3, ["x1", "x2", "x3"])
    # 仅 x2 是 multi_user_impact 且未转人工 -> 多人影响误判=1。
    assert multi_user_misclassified(rows) == (1, 1, ["x2"])
    # 仅 x3 无证据且允许自动回复 -> 无证据自动回复=1。
    assert no_evidence_auto_reply(rows) == (1, 3, ["x3"])


def test_crafted_failing_sample_triggers_gate_failure():
    """构造会让门禁失败的样本，run_frozen_metrics 必须识别 all_gates_pass=False。"""
    failing = FrozenSample(
        id="bad-01",
        text="账号正常、网关正常，有知识库可给排障步骤。",
        fault="connection_failed",
        hypothesis="connection_failed",
        evidence_quote="有知识库",
        requires_human=True,  # gold 认为必须人工
        has_evidence=True,  # 但规则会因知识命中而允许自动
        provided_fields={"account_status": "active", "gateway_status": "up"},
    )
    report = run_frozen_metrics([failing])
    # 该样本被预测为 requires_human=False（自动）而 gold 要求人工 -> 高风险误自动=1。
    assert report["metrics"]["high_risk_false_auto"]["value"] == 1
    assert report["all_gates_pass"] is False


def test_build_evidence_from_sample():
    samples = load_frozen_samples(_DEFAULT_PATH)
    s = next(x for x in samples if x.id == "fs-001")
    ev = build_evidence_from_sample(s)
    assert ev.fault == "connection_failed"
    assert ev.account_status.value == "active"
    assert ev.gateway_status.value == "up"
    assert ev.error_code == "809"
    assert ev.knowledge_hit is True
    assert ev.evidence_quote == "提示错误码809"
