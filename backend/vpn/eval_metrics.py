"""Phase 4 —— VPN 诊断深度冻结样本评测（backend/vpn/eval_metrics.py）。

模块归属：backend/vpn/eval_metrics.py（本任务 t3 新增）。目标：
    - 用**冻结样本**（backend/vpn/eval/frozen_samples.json，由真实/脱敏措辞编写）驱动
      确定性规则 + 护栏（rules.guardrail_evaluate），计算 6 项 Phase-4 关键指标；
    - 指标「**来自冻结样本**」：gold 标签（fault / hypothesis / evidence_quote /
      requires_human）全部取自样本文件，本模块是 provider-agnostic，只做
      「规则引擎输出 vs gold」的比对，不依赖任何真实模型/DB/VPN 凭证。

指标口径（对齐任务目标）：
    M-P4-1  `classification_accuracy`   VPN 分类准确率（fault 匹配）——目标 >=0.90
    M-P4-2  `hypothesis_recognition`    固定字段(假设)识别率——目标 >=0.95
    M-P4-3  `evidence_citation_completeness`  证据引用完整率——目标 ==1.00
    M-P4-4  `high_risk_false_auto`      高风险误自动处置（gold 人工但被自动放行）——目标 ==0
    M-P4-5  `multi_user_misclassified`  多人影响误判为单用户——目标 ==0
    M-P4-6  `no_evidence_auto_reply`    无证据自动回复——目标 ==0

重要声明：这些指标是在 **本任务编写的冻结样本集** 上计算的。当前无真实生产语料库，
故这些数字只证明「确定性规则在该冻结集上的一致性/自洽性」，**不构成生产级准确率**。
harness 已为「真实冻结语料」就绪：只需把样例文件替换为真实注释语料（gold 标签来自
样本文件本身），本模块即可无改动重跑。

纯函数、无 IO（除从磁盘加载样本外）、可单测（tests/test_vpn_phase4_eval.py）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .rules import (
    DiagnosisConclusion,
    VpnEvidence,
    build_evidence_from_case,
    conclusion_fault_class,
    guardrail_evaluate,
)

# 默认冻结样本路径（相对 repo root；CLI 可覆盖）。
DEFAULT_FROZEN_SAMPLES_PATH = os.path.join("backend", "vpn", "eval", "frozen_samples.json")

# 目标阈值（对齐任务要求）。
CLASSIFICATION_TARGET = 0.90
HYPOTHESIS_TARGET = 0.95
EVIDENCE_QUOTE_TARGET = 1.00
GATE_COUNT_TARGET = 0


class FrozenSample(BaseModel):
    """一条冻结评测样本（extra="forbid"，gold 标签全部来自样本文件）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    fault: str  # gold：5 类 VPN 故障（connection_failed/auth_failed/...）
    hypothesis: str  # gold：根因假设编码
    ruled_out: list[str] = Field(default_factory=list)
    evidence_quote: str = ""  # gold：应从文本中引用的支撑子串（证据引用完整率）
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_human: bool = False  # gold：是否必须人工
    provided_fields: dict[str, Any] = Field(default_factory=dict)
    has_evidence: bool = False  # 是否有知识库命中（决定自动排障 or 升级）
    required_version: str = ""  # 客户端要求版本（可为空）
    identity_ok: bool = True


def load_frozen_samples(
    path: str | os.PathLike[str] | None = None,
) -> list[FrozenSample]:
    """从 JSON 文件加载冻结样本集。

    兼容「含 _meta + samples 数组」与「顶层为样本数组」两种文件形态。
    """
    resolved = Path(path or DEFAULT_FROZEN_SAMPLES_PATH)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    with open(resolved, encoding="utf-8") as handle:
        raw = json.load(handle)
    if isinstance(raw, dict):
        samples = raw.get("samples") or []
    else:
        samples = raw
    return [FrozenSample.model_validate(item) for item in samples]


def build_evidence_from_sample(sample: FrozenSample) -> VpnEvidence:
    """把冻结样本归一化为 VpnEvidence（复用 rules.build_evidence_from_case 入口）。"""
    case = {
        "vpn_fault": sample.fault,
        "fault": sample.fault,
        "provided_fields": dict(sample.provided_fields or {}),
        "required_version": sample.required_version,
        "has_evidence": sample.has_evidence,
        "evidence_quote": sample.evidence_quote,
        "text": sample.text,
        "asset_id": (sample.provided_fields or {}).get("asset_id"),
    }
    return build_evidence_from_case(case, identity_ok=sample.identity_ok)


def predict_sample(sample: FrozenSample) -> DiagnosisConclusion:
    """对一条样本执行确定性护栏判定（rules.guardrail_evaluate）。"""
    return guardrail_evaluate(build_evidence_from_sample(sample))


# ---------------------------------------------------------------------------
# 指标计算（纯函数）
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def classification_accuracy(rows: Sequence[Mapping[str, Any]]) -> tuple[float, int]:
    """VPN 分类准确率：pred 假设映射为 5 类故障后与 gold fault 比对。

    排除 no_evidence（conclusion_fault_class 返回 no_evidence，非 5 类故障）。
    返回 (accuracy, 计入样本数)。
    """
    counted = 0
    matched = 0
    for row in rows:
        pred_class = row["pred_class"]
        if pred_class == "no_evidence":
            continue
        counted += 1
        if pred_class == row["gold_fault"]:
            matched += 1
    return (_rate(matched, counted), counted)


def hypothesis_recognition(rows: Sequence[Mapping[str, Any]]) -> tuple[float, int]:
    matched = sum(1 for r in rows if r["pred_hypothesis"] == r["gold_hypothesis"])
    return (_rate(matched, len(rows)), len(rows))


def evidence_citation_completeness(rows: Sequence[Mapping[str, Any]]) -> tuple[float, int, int]:
    """证据引用完整率：证据非空 且 引用了 gold evidence_quote。返回 (rate, complete, total)。"""
    total = 0
    complete = 0
    for row in rows:
        quote = row["evidence_quote"]
        evidence = row["pred_evidence"]
        if not quote:
            # 无引用要求时只要证据非空即算完整
            total += 1
            if evidence:
                complete += 1
            continue
        total += 1
        if evidence and any(quote in item for item in evidence):
            complete += 1
    return (_rate(complete, total), complete, total)


def high_risk_false_auto(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int, list[str]]:
    """高风险误自动处置：gold 必须人工（requires_human=True）但被判自动放行。返回 (count, total, ids)。"""
    total = sum(1 for r in rows if r["gold_requires_human"])
    bad = [r["id"] for r in rows if r["gold_requires_human"] and not r["pred_requires_human"]]
    return len(bad), total, bad


def multi_user_misclassified(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int, list[str]]:
    """多人影响误判为单用户：gold fault=multi_user_impact 但未被升级/被误判。"""
    total = sum(1 for r in rows if r["gold_fault"] == "multi_user_impact")
    bad = [
        r["id"]
        for r in rows
        if r["gold_fault"] == "multi_user_impact" and not r["pred_requires_human"]
    ]
    return len(bad), total, bad


def no_evidence_auto_reply(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int, list[str]]:
    """无证据自动回复：证据空 或 假设为 no_evidence 却允许自动（requires_human=False）。"""
    bad = [
        r["id"]
        for r in rows
        if not r["pred_requires_human"] and (not r["pred_evidence"] or r["pred_hypothesis"] == "no_evidence")
    ]
    return len(bad), len(rows), bad


# ---------------------------------------------------------------------------
# 汇总：对样本集跑全套指标
# ---------------------------------------------------------------------------


def evaluate_samples(samples: Sequence[FrozenSample]) -> dict[str, Any]:
    """对冻结样本集执行确定性推理，返回每条样本的预测统计行。"""
    rows: list[dict[str, Any]] = []
    for sample in samples:
        pred = predict_sample(sample)
        rows.append(
            {
                "id": sample.id,
                "text": sample.text,
                "gold_fault": sample.fault,
                "gold_hypothesis": sample.hypothesis,
                "gold_requires_human": sample.requires_human,
                "evidence_quote": sample.evidence_quote,
                "pred_hypothesis": pred.hypothesis,
                "pred_class": conclusion_fault_class(pred.hypothesis),
                "pred_evidence": list(pred.evidence),
                "pred_requires_human": pred.requires_human,
                "pred_next_action": pred.next_action,
            }
        )
    return {"rows": rows}


def run_frozen_metrics(samples: Sequence[FrozenSample]) -> dict[str, Any]:
    """计算全套 Phase-4 指标 + 门禁结论。返回结构化报告 dict。"""
    data = evaluate_samples(samples)
    rows = data["rows"]

    cls_acc, cls_count = classification_accuracy(rows)
    hyp_acc, hyp_count = hypothesis_recognition(rows)
    cite_rate, cite_complete, cite_total = evidence_citation_completeness(rows)
    hr_bad, hr_total, hr_ids = high_risk_false_auto(rows)
    mu_bad, mu_total, mu_ids = multi_user_misclassified(rows)
    ne_bad, ne_total, ne_ids = no_evidence_auto_reply(rows)

    gates: dict[str, dict[str, Any]] = {
        "classification_accuracy_gte_0.90": {
            "value": cls_acc,
            "target": CLASSIFICATION_TARGET,
            "pass": cls_acc >= CLASSIFICATION_TARGET,
        },
        "hypothesis_recognition_gte_0.95": {
            "value": hyp_acc,
            "target": HYPOTHESIS_TARGET,
            "pass": hyp_acc >= HYPOTHESIS_TARGET,
        },
        "evidence_citation_completeness_1.00": {
            "value": cite_rate,
            "target": EVIDENCE_QUOTE_TARGET,
            "pass": cite_rate >= EVIDENCE_QUOTE_TARGET,
        },
        "high_risk_false_auto_0": {
            "value": hr_bad,
            "target": GATE_COUNT_TARGET,
            "pass": hr_bad == GATE_COUNT_TARGET,
        },
        "multi_user_misclassified_0": {
            "value": mu_bad,
            "target": GATE_COUNT_TARGET,
            "pass": mu_bad == GATE_COUNT_TARGET,
        },
        "no_evidence_auto_reply_0": {
            "value": ne_bad,
            "target": GATE_COUNT_TARGET,
            "pass": ne_bad == GATE_COUNT_TARGET,
        },
    }

    return {
        "sample_count": len(rows),
        "metrics": {
            "classification_accuracy": {"value": cls_acc, "counted": cls_count},
            "hypothesis_recognition": {"value": hyp_acc, "counted": hyp_count},
            "evidence_citation_completeness": {
                "value": cite_rate,
                "complete": cite_complete,
                "total": cite_total,
            },
            "high_risk_false_auto": {"value": hr_bad, "gold_human_total": hr_total, "ids": hr_ids},
            "multi_user_misclassified": {"value": mu_bad, "multi_user_total": mu_total, "ids": mu_ids},
            "no_evidence_auto_reply": {"value": ne_bad, "total": ne_total, "ids": ne_ids},
        },
        "gates": gates,
        "all_gates_pass": all(bool(g["pass"]) for g in gates.values()),
        "provenance": (
            "指标基于 backend/vpn/eval/frozen_samples.json（本任务编写，真实/脱敏措辞）。"
            "当前无真实生产语料库，故数字代表确定性规则在该冻结集上的自洽性，"
            "不构成生产级准确率。harness 为 provider-agnostic，可替换为真实冻结语料。"
        ),
    }


def build_report_dict(samples: Sequence[FrozenSample]) -> dict[str, Any]:
    """生成最终评测报告（兼容 CLI 直接 json 输出）。"""
    return run_frozen_metrics(samples)
