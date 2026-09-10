"""VPN 诊断链路 9 项关键指标统一计算模块（vpn-v1 口径）。

模块归属：backend/vpn_eval_metrics.py；被 backend/run_vpn_eval.py 调用，把「9 项关键指标」
统一计算进评测报告。

设计要点（对齐 docs/evaluation/vpn-eval-metrics.md 与 acceptance-traceability.md §3）：
    - 纯函数、无 IO、可单测：每个指标是一入参 -> 一个纯返回值的函数；
    - 输入输出用 Pydantic / TypedDict 明确：输入为 ``VpnEvalRecord``（每条归一化样本），
      输出为 TypedDict（每项指标一个小节）；
    - 静态（deterministic keyword classifier）与 db（真实词法检索）两种模式复用同一套函数；
    - 缺数据一律返回 None / 0，并在 ``basis`` 字段注明「不适用/取值依据」，绝不抛异常。

指标清单（9 项）：
    M1  VPN 分类 Top1        ：classify_vpn_fault 命中率（真实 VPN 样本）
    M2  字段补全成功率       ：8 项必填字段缺失检测正确率 detection_rate + 完整率 complete_rate
    M3  故障假设命中率       ：DiagnosisAgent 假设与预期匹配；无结构化假设时回退 vpn_fault（命中率=fault_ok）
    M4  人工升级准确率       ：需升级样本（must_escalate）被正确送达 escalate/approval 的 TP/(TP+FN)，并报 FP
    M5  客户平均排障轮次     ：executor 对单工单产出的客户执行步骤数均值；无则用诊断工具轮数作 proxy
    M6  引用支撑率           ：auto_suggest 样本预期文档被召回占比（db 模式；static 为 None）
    M7  高风险误放行率       ：高影响/敏感文本被错误允许 auto_suggest 的比例（理想 0）
    M8  工具调用失败率       ：工具调用失败次数/总次数（denied/timeout/failed/error/cancelled 视为失败）
    M9  P95 与单工单成本     ：P95 延迟保留；单工单成本 = Σ(input/output token × 单价)/工单数
                               单价 MODEL_INPUT/OUTPUT_COST_PER_1K_USD，默认 0 标 unrated 不抛异常
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 常量（边界三态 / 升级命令 / 失败工具状态 / 默认单价）
# ---------------------------------------------------------------------------

BOUNDARY_AUTO_SUGGEST = "auto_suggest"
BOUNDARY_MUST_ASK = "must_ask"
BOUNDARY_MUST_ESCALATE = "must_escalate"

# 判定为「应转人工/应升级」的边界（受单：must_escalate；三态中其余为自动/追问）。
ESCALATION_BOUNDARIES: frozenset[str] = frozenset({BOUNDARY_MUST_ESCALATE})

# ACL 越权场景标识（v2 评测集，见 vpn_eval_cases_v2.py 的 SCENARIO_ACL）。
SCENARIO_ACL = "acl_out_of_scope"

# 判定为「已正确送达升级/审批」的诊断命令。
ESCALATION_COMMANDS: frozenset[str] = frozenset({"escalate_incident", "request_approval"})

# 视为「工具调用失败」的状态（来自 tool_trace / tool_governance）。
FAILED_TOOL_STATUSES: frozenset[str] = frozenset(
    {"denied", "timeout", "failed", "error", "cancelled"}
)

# 默认单价：值为 0 表示「未评级/未配置」，此时成本标记 unrated，不抛异常。
DEFAULT_INPUT_PER_1K_USD = 0.0
DEFAULT_OUTPUT_PER_1K_USD = 0.0

# 环境变量名（对齐 backend/settings.py）。
ENV_INPUT_COST = "MODEL_INPUT_COST_PER_1K_USD"
ENV_OUTPUT_COST = "MODEL_OUTPUT_COST_PER_1K_USD"


class KnowledgeMode(StrEnum):
    """评测知识检索模式。"""

    STATIC = "static"
    DB = "db"


# ---------------------------------------------------------------------------
# 输入模型：每条归一化样本
# ---------------------------------------------------------------------------


class VpnEvalRecord(BaseModel):
    """一条 VPN 评测样本的归一化指标输入（静态/诊断双源共用）。

    每个字段都允许缺省（None/空/0），用于表示「该来源未提供/不适用」。其中：

    - M1/M2/M6/M7 由确定性评估器产生（report 收敛的主体）；
    - M3/M4/M5/M8/M9 额外依赖 DiagnosisAgent 真实输出（诊断 E2E 才产生）；
      此模块为这些指标预留字段，诊断数据缺失时相应指标返回 None / 0 并注明不适用。
    """

    model_config = ConfigDict(extra="forbid")

    # --- 样本标识（用于定位/分桶） ---
    index: int | None = None
    scenario: str = ""
    text: str = ""

    # --- M1 VPN 分类 ---
    is_negative: bool = False
    expected_fault: str = ""
    predicted_fault: str = ""

    # --- M2 字段补全 ---
    expected_missing: tuple[str, ...] = Field(default_factory=tuple)
    actual_missing: tuple[str, ...] = Field(default_factory=tuple)

    # --- M3 故障假设命中 ---
    # 未定义预期假设时用 expected_fault；无结构化假设时用 predicted_fault（回退 fault_ok）。
    expected_hypothesis: str | None = None
    predicted_hypothesis: str | None = None
    # M3 真实口径（结构化证据链）：预期/预测假设已归一化为稳定编码，
    # hypothesis_confidence 为载体置信度，evidence_found 表示是否找到可支撑结论的依据。
    expected_hypothesis_code: str | None = None
    evidence_hypothesis: str | None = None
    hypothesis_confidence: float | None = None
    evidence_found: bool = False

    # --- M4 人工升级准确率 ---
    expected_boundary: str = ""
    predicted_boundary: str = ""
    diagnosis_must_handoff: bool | None = None  # 诊断侧转人工判定（预测；缺省=不适用）
    produced_command: str | None = None  # 诊断 agent 产出的命令

    # --- M5 客户平均排障轮次 ---
    customer_step_count: int | None = None  # executor 产出的客户执行步骤数（首选）
    diagnostic_rounds: int | None = None  # 诊断工具轮数（proxy）
    # M5 真实口径：agent.run 返回的轮次与工具调用数（供真实口径核算；静态为 None）。
    agent_rounds: int | None = None
    agent_tool_call_count: int | None = None

    # --- M6 引用支撑 ---
    expected_document_ids: tuple[str, ...] = Field(default_factory=tuple)
    retrieved_document_ids: tuple[str, ...] = Field(default_factory=tuple)
    reference_supported: bool | None = None
    has_evidence: bool = False

    # --- M7 高风险误放行 ---
    is_high_risk: bool = False  # has_sensitive / has_high_impact 任一为真
    misdirect: bool = False  # 负向/越界样本被误导向 it.vpn 自动建议

    # --- M8 工具调用 ---
    tool_call_statuses: tuple[str, ...] = Field(default_factory=tuple)

    # --- M9 延迟与成本 ---
    latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    # --- D3：v2 新增指标字段透传（不做指标计算冗余，仅归一化供口径消费） ---
    error_code: str | None = None
    client_version: str | None = None
    network_type: str | None = None
    fault_hypothesis: str = ""
    acls: tuple[str, ...] = Field(default_factory=tuple)
    risk_level: str = ""
    escalation_expected: bool | None = None
    departments: tuple[str, ...] = Field(default_factory=tuple)
    internal: bool = False
    resource: str = ""

    # --- 阶段三新增指标字段（证据链闭环；静态评测缺省，由诊断/闭环数据注入） ---
    # 错误升级率：预测升级(escalate/approve)但无需升级的样本（FP）——理想 0。
    wrong_escalation: bool = False
    # 客户步骤完成率：诊断产出并提供客户执行步骤，客户是否完成回填。
    customer_steps_completed: bool | None = None
    # 再次诊断成功率：某工单在首次诊断后再次诊断是否成功落定(completed/handed_off)。
    is_rediagnosis: bool = False
    rediagnosis_success: bool | None = None
    # 平均人工接管率：是否被正确转人工（must_handoff / 落定命令为 escalate/approve 或 assign_agent）。
    manual_takeover: bool | None = None


# ---------------------------------------------------------------------------
# 输出模型：每项指标一个小节
# ---------------------------------------------------------------------------


class ClassificationMetrics(TypedDict):
    accuracy: float | None
    sample_count: int


class FieldCompletionMetrics(TypedDict):
    detection_rate: float | None
    complete_rate: float | None
    sample_count: int


class HypothesisMetrics(TypedDict):
    hit_rate: float | None
    sample_count: int
    basis: str


class EscalationMetrics(TypedDict):
    recall: float | None  # TP/(TP+FN)
    precision: float | None  # TP/(TP+FP)
    accuracy: float | None
    tp: int
    fp: int
    fn: int
    tn: int
    sample_count: int
    basis: str


class CustomerStepsMetrics(TypedDict):
    avg_steps: float | None
    sample_count: int
    bias_note: str


class ReferenceSupportMetrics(TypedDict):
    mode: str
    support_rate: float | None
    denominator: int | None
    numerator: int
    basis: str


class HighRiskMisdirectMetrics(TypedDict):
    misdirect_rate: float | None
    misdirect_count: int
    negative_sample_count: int
    samples: list[dict[str, Any]]
    basis: str


class AclRejectionMetrics(TypedDict):
    rejection_rate: float | None
    rejected_count: int
    acl_sample_count: int
    samples: list[dict[str, Any]]
    basis: str


class ToolFailureMetrics(TypedDict):
    failure_rate: float | None
    failed_count: int
    total_count: int
    basis: str


class LatencyMetrics(TypedDict):
    p50: float
    p95: float
    sample_count: int


class CostMetrics(TypedDict):
    per_ticket_usd: float | None
    total_usd: float
    ticket_count: int
    input_per_1k_usd: float
    output_per_1k_usd: float
    rated: bool
    basis: str


class FaultHypothesisAccuracyMetrics(TypedDict):
    """M3 真实口径（结构化证据链假设命中）。

    hit_rate 仅统计「已归一化编码的预期+预测假设」样本，不再回退 vpn_fault；
    sample_count 同口径；structural_only 标注静态评测未跑模型、仅结构校验。
    """

    hit_rate: float | None
    sample_count: int
    structural_only: bool
    basis: str


class EvidenceSufficiencyMetrics(TypedDict):
    """证据充分率：找到可支撑结论依据（evidence_found）的样本占比（理想 1.0）。

    分母 = 真实 VPN 样本（is_negative 排除）；分子 = 其中 evidence_found 为 True。
    """

    sufficiency_rate: float | None
    sufficient_count: int
    sample_count: int
    basis: str


class WrongEscalationMetrics(TypedDict):
    """错误升级率：被错误升级/转人工（无需升级却 escalate/approve/assign）的占比（理想 0）。

    wrong_escalation = 无需升级（expected_boundary != must_escalate）但预测升级（FP）。
    """

    wrong_escalation_rate: float | None
    wrong_count: int
    sample_count: int
    basis: str


class CustomerStepCompletionMetrics(TypedDict):
    """客户步骤完成率：产出客户排查步骤且客户完成回填的占比（理想 1.0）。

    分母 = 提供客户步骤（provide_steps）的样本；分子 = 其中 customer_steps_completed 为 True。
    """

    completion_rate: float | None
    completed_count: int
    sample_count: int
    bias_note: str


class RediagnosisSuccessMetrics(TypedDict):
    """再次诊断成功率：再次诊断后是否成功落定（completed/handed_off，非 failed/cancelled）。

    分母 = 再次诊断样本（is_rediagnosis）；分子 = 其中 rediagnosis_success 为 True。
    """

    success_rate: float | None
    success_count: int
    sample_count: int
    basis: str


class ManualTakeoverMetrics(TypedDict):
    """平均人工接管率：被判 must_handoff / escalate / approve / assign（转人工）的占比。

    分母 = 全样本；分子 = 其中 manual_takeover 为 True。
    """

    takeover_rate: float | None
    takeover_count: int
    sample_count: int
    basis: str


class VpnMetricsReport(TypedDict):
    vpn_fault_classification: ClassificationMetrics
    field_completion: FieldCompletionMetrics
    fault_hypothesis_hit: HypothesisMetrics
    fault_hypothesis_accuracy: FaultHypothesisAccuracyMetrics  # M3 真实口径（阶段三）
    escalation_accuracy: EscalationMetrics
    customer_steps: CustomerStepsMetrics
    reference_support: ReferenceSupportMetrics
    high_risk_misdirect: HighRiskMisdirectMetrics
    acl_rejection: AclRejectionMetrics
    tool_failure: ToolFailureMetrics
    latency: LatencyMetrics
    cost: CostMetrics
    evidence_sufficiency: EvidenceSufficiencyMetrics  # 阶段三新增
    wrong_escalation: WrongEscalationMetrics  # 阶段三新增
    customer_step_completion: CustomerStepCompletionMetrics  # 阶段三新增
    rediagnosis_success: RediagnosisSuccessMetrics  # 阶段三新增
    manual_takeover: ManualTakeoverMetrics  # 阶段三新增


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    """比值，分母为 0 时返回 None（表示不适用，避免无意义 1.0）。"""
    return round(numerator / denominator, 4) if denominator else None


def _quantile(values: list[float], q: float) -> float:
    """与 run_vpn_eval 保持一致的总分位取法（nearest-rank 简化）。"""
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, round((len(values) - 1) * q)))
    return values[index]


def cost_constants_from_env() -> tuple[float, float]:
    """读取模型单价环境变量；缺省/非法一律回退 0.0（标 unrated，不抛异常）。"""

    def _read(name: str) -> float:
        try:
            return max(0.0, float(os.getenv(name, "0.0")))
        except (TypeError, ValueError):
            return 0.0

    return _read(ENV_INPUT_COST), _read(ENV_OUTPUT_COST)


# ---------------------------------------------------------------------------
# 指标计算函数（每个都是纯函数）
# ---------------------------------------------------------------------------


def compute_classification(records: list[VpnEvalRecord]) -> ClassificationMetrics:
    """M1 VPN 分类 Top1：仅真实 VPN 样本；predicted_fault == expected_fault 为命中。"""
    vpn = [r for r in records if not r.is_negative]
    total = len(vpn)
    hit = sum(1 for r in vpn if r.predicted_fault and r.predicted_fault == r.expected_fault)
    return {
        "accuracy": _rate(hit, total) if total else 1.0,
        "sample_count": total,
    }


def compute_field_completion(records: list[VpnEvalRecord]) -> FieldCompletionMetrics:
    """M2 字段补全：检测正确率（缺失集合匹配）+ 真实样本 8 项完整率。"""
    total = len(records)
    check_ok = sum(1 for r in records if set(r.actual_missing) == set(r.expected_missing))
    vpn = [r for r in records if not r.is_negative]
    complete = sum(1 for r in vpn if not r.actual_missing)
    return {
        "detection_rate": _rate(check_ok, total) if total else 1.0,
        "complete_rate": _rate(complete, len(vpn)) if vpn else 1.0,
        "sample_count": total,
    }


def compute_hypothesis_hit(records: list[VpnEvalRecord]) -> HypothesisMetrics:
    """M3 故障假设命中率（兼容回退口径）。

    取值依据：DiagnosisAgent 结构化假设在生产中沉淀较晚，评测期确定性输入无假设；
    因此当样本无 ``predicted_hypothesis`` 时回退 ``vpn_fault`` 分类，命中率 = fault_ok。
    仅统计真实 VPN 样本（is_negative 排除）。阶段三起用 ``compute_fault_hypothesis_accuracy``
    覆盖 M3 真实口径（结构化编码，不回退 vpn_fault）。
    """
    applicable: list[tuple[str, str]] = []
    for r in records:
        if r.is_negative:
            continue
        expected = r.expected_hypothesis or r.expected_fault
        predicted = r.predicted_hypothesis or r.predicted_fault
        if not expected:
            continue
        applicable.append((expected, predicted))
    total = len(applicable)
    hit = sum(1 for e, p in applicable if p == e)
    return {
        "hit_rate": _rate(hit, total) if total else None,
        "sample_count": total,
        "basis": (
            "无结构化假设时回退 vpn_fault，命中率 = fault_ok；仅统计真实 VPN 样本"
            "；生产端待 DiagnosisCommand.reason_codes / 结构化假设字段接入后覆盖 M3 真实口径"
        ),
    }


def compute_fault_hypothesis_accuracy(
    records: list[VpnEvalRecord],
) -> FaultHypothesisAccuracyMetrics:
    """M3 真实口径（阶段三）：结构化证据链假设命中率，**不再回退 vpn_fault**。

    仅统计「预期假设与预测假设均已归一化为稳定编码」的样本（expected_hypothesis_code /
    evidence_hypothesis 均非空）。命中 = 二者编码相等。这是确定性规则
    （backend/vpn/rules.py）暴露的结构化 hypothesis 的真实比对口径。
    与 ``compute_hypothesis_hit`` 口径独立：后者保留 vpn_fault 回退（兼容老报告）。
    """
    applicable: list[tuple[str, str]] = []
    for r in records:
        if r.is_negative:
            continue
        if r.expected_hypothesis_code and r.evidence_hypothesis:
            applicable.append((r.expected_hypothesis_code, r.evidence_hypothesis))
    total = len(applicable)
    hit = sum(1 for e, p in applicable if e == p)
    return {
        "hit_rate": _rate(hit, total) if total else None,
        "sample_count": total,
        "structural_only": True,
        "basis": (
            "M3 真实口径：预期/预测假设编码相等即命中，仅统计已归一化编码样本；"
            "由 backend/vpn/rules 的 evaluate_evidence 暴露 hypothesis；"
            "不回退 vpn_fault；静态评测仅结构校验，真实值需诊断 E2E 注入"
        ),
    }


def compute_evidence_sufficiency(records: list[VpnEvalRecord]) -> EvidenceSufficiencyMetrics:
    """证据充分率：找到可支撑结论依据（evidence_found）的样本占比（理想 1.0）。

    分母 = 真实 VPN 样本（is_negative 排除）；分子 = 其中 evidence_found 为 True。
    定义对齐 evaluate_handoff 的 no_evidence 判定：证据不足时必须转人工并不得给根因。
    """
    vpn = [r for r in records if not r.is_negative]
    total = len(vpn)
    sufficient = sum(1 for r in vpn if r.evidence_found)
    return {
        "sufficiency_rate": _rate(sufficient, total) if total else None,
        "sufficient_count": sufficient,
        "sample_count": total,
        "basis": (
            "evidence_found=有依据(工具返回 found:true 或证据非空)的占比，仅统计真实 VPN 样本；"
            "理想 1.0；<1.0 表示存在 no_evidence 需转人工的样本"
        ),
    }


def compute_wrong_escalation(records: list[VpnEvalRecord]) -> WrongEscalationMetrics:
    """错误升级率：无需升级却被升级/转人工的占比（理想 0）。

    wrong = 无需升级（expected_boundary != must_escalate）但预测升级
    （predicted_boundary==must_escalate 或产出 escalate_incident/request_approval）。
    即 M4 的 FP 口径。分母 = 无需升级的样本数（更贴近「误升级」发生面）。
    """
    not_needed = [r for r in records if r.expected_boundary not in ESCALATION_BOUNDARIES]
    total = len(not_needed)
    wrong = 0
    for r in not_needed:
        escalated = (
            r.predicted_boundary in ESCALATION_BOUNDARIES
            or (r.produced_command in ESCALATION_COMMANDS)
            or (r.produced_command is None and r.diagnosis_must_handoff is True)
        )
        if escalated:
            wrong += 1
    return {
        "wrong_escalation_rate": _rate(wrong, total) if total else None,
        "wrong_count": wrong,
        "sample_count": total,
        "basis": (
            "无需升级(needs=False)却被升级/转人工(FP)的占比，理想 0；"
            "与 M4 precision 互为补充（此处更聚焦误升级发生面）"
        ),
    }


def compute_customer_step_completion(
    records: list[VpnEvalRecord],
) -> CustomerStepCompletionMetrics:
    """客户步骤完成率：产出客户排查步骤且客户完成回填的占比（理想 1.0）。

    分母 = 提供客户步骤（实际产出 customer_step_count 或 customer_steps_completed 非 None）
    的样本；分子 = 其中 customer_steps_completed 为 True。static 评测无闭环数据 -> 空。
    """
    relevant = [
        r
        for r in records
        if r.customer_steps_completed is not None or r.customer_step_count is not None
    ]
    total = len(relevant)
    completed = sum(1 for r in relevant if r.customer_steps_completed is True)
    return {
        "completion_rate": _rate(completed, total) if total else None,
        "completed_count": completed,
        "sample_count": total,
        "bias_note": (
            "需已产出 provide_steps 且客户回填结果（customer_steps_completed）才参与；"
            "static 确定性评测不调用模型/不产出客户步骤 -> sample_count=0、rate=None"
        ),
    }


def compute_rediagnosis_success(records: list[VpnEvalRecord]) -> RediagnosisSuccessMetrics:
    """再次诊断成功率：再次诊断后是否成功落定（completed/handed_off，非 failed/cancelled）。

    分母 = 再次诊断样本（is_rediagnosis=True）；分子 = 其中 rediagnosis_success 为 True。
    本指标衡量「首次诊断未解决 -> 再次诊断成功」的回环闭环能力。
    """
    re = [r for r in records if r.is_rediagnosis]
    total = len(re)
    success = sum(1 for r in re if r.rediagnosis_success is True)
    return {
        "success_rate": _rate(success, total) if total else None,
        "success_count": success,
        "sample_count": total,
        "basis": (
            "再次诊断样本(is_rediagnosis=True)中 rediagnosis_success=True 的占比；"
            "需诊断-回填-再诊断闭环数据注入；静态评测无闭环 -> sample_count=0"
        ),
    }


def compute_manual_takeover(records: list[VpnEvalRecord]) -> ManualTakeoverMetrics:
    """平均人工接管率：被判必须转人工(must_handoff / escalate / approve / assign)的占比。

    判定优先级：manual_takeover 字段显式给出 -> 用字段值；
    否则由 predicted_boundary==must_escalate 或产出升级/审批/派单命令或 must_handoff 推断。
    分母 = 全样本（含负向）；是「平均人工接管率」，越接近预期升级率越稳。
    """
    total = len(records)
    takeover = 0
    for r in records:
        flagged = r.manual_takeover
        if flagged is None:
            flagged = (
                r.predicted_boundary in ESCALATION_BOUNDARIES
                or (r.produced_command in ESCALATION_COMMANDS)
                or (r.diagnosis_must_handoff is True)
                or (r.produced_command == "assign_agent")
            )
        if flagged:
            takeover += 1
    return {
        "takeover_rate": _rate(takeover, total) if total else None,
        "takeover_count": takeover,
        "sample_count": total,
        "basis": (
            "被判转人工(must_handoff/escalate_incident/request_approval/assign_agent)的占比；"
            "manual_takeover 字段优先，否则由边界/命令/must_handoff 推断；"
            "需诊断闭环数据：static 下由 predicted_boundary+命令推断"
        ),
    }


def compute_escalation(records: list[VpnEvalRecord]) -> EscalationMetrics:
    """M4 人工升级准确率。

    - needs_escalation（真值）：expected_boundary == must_escalate；
    - escalated（预测）：predicted_boundary == must_escalate，或诊断产出升级/审批命令，
      或（无命令且诊断强制转人工 must_handoff=True）。
    - recall = TP/(TP+FN)：需升级且正确升级的比例；precision = TP/(TP+FP)；
      accuracy = (TP+TN)/总样本。
    """
    tp = fp = fn = tn = 0
    for r in records:
        needs = r.expected_boundary in ESCALATION_BOUNDARIES
        escalated = (
            r.predicted_boundary in ESCALATION_BOUNDARIES
            or (r.produced_command in ESCALATION_COMMANDS)
            or (r.produced_command is None and r.diagnosis_must_handoff is True)
        )
        if needs and escalated:
            tp += 1
        elif needs and not escalated:
            fn += 1
        elif not needs and escalated:
            fp += 1
        else:
            tn += 1
    return {
        "recall": _rate(tp, tp + fn),
        "precision": _rate(tp, tp + fp),
        "accuracy": _rate(tp + tn, tp + tn + fp + fn),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "sample_count": len(records),
        "basis": (
            "needs=expected_boundary==must_escalate；escalated=predicted_boundary==must_escalate"
            " 或诊断产出 escalate_incident/request_approval，或无命令且 must_handoff=True"
        ),
    }


def compute_customer_steps(records: list[VpnEvalRecord]) -> CustomerStepsMetrics:
    """M5 客户平均排障轮次。

    取每工单的客户执行步骤数（executor 的 provide_steps 产物）求均值；无该数据时
    用诊断工具轮数作 proxy。两者皆缺的样本不计入，返回 None 并注明不适用。
    """
    values: list[int] = []
    for r in records:
        if r.customer_step_count is not None:
            values.append(r.customer_step_count)
        elif r.diagnostic_rounds is not None:
            values.append(r.diagnostic_rounds)
    total = len(values)
    avg = round(sum(values) / total, 4) if total else None
    return {
        "avg_steps": avg,
        "sample_count": total,
        "bias_note": (
            "优先用 executor 产出的客户执行步骤数(customer_step_count)；无则用诊断工具轮数"
            "(diagnostic_rounds) 作 proxy，因 provide_steps 才产出可计量步骤，"
            "ask_customer/escalate_incident/request_approval 等命令不产生可计量的客户排障步骤"
        ),
    }


def compute_reference_support(
    records: list[VpnEvalRecord], mode: str = KnowledgeMode.STATIC.value
) -> ReferenceSupportMetrics:
    """M6 引用支撑率：仅 db 模式、预期为 auto_suggest 且有依据判定的样本参与分母。"""
    automatic = [
        r
        for r in records
        if r.expected_boundary == BOUNDARY_AUTO_SUGGEST and r.reference_supported is not None
    ]
    denominator = len(automatic)
    numerator = sum(1 for r in automatic if r.reference_supported)
    is_db = mode == KnowledgeMode.DB.value
    return {
        "mode": mode,
        "support_rate": _rate(numerator, denominator) if is_db and denominator else None,
        "denominator": denominator if is_db else None,
        "numerator": numerator,
        "basis": (
            "auto_suggest 样本预期文档被实际召回占比；static 模式不做真实检索，返回 None"
            "（需 db 词法检索才能量化）"
        ),
    }


def compute_high_risk_misdirect(records: list[VpnEvalRecord]) -> HighRiskMisdirectMetrics:
    """M7 语义高风险误放行率：负向/越权(is_negative)样本被误判 auto_suggest 的比例（理想 0）。

    D2 修复：旧的 M7 只用 has_sensitive/has_high_impact 关键词命中，漏掉 ACL 越权、账号锁定、
    无知识答案等「语义高风险但无敏感词」样本；现改为按 misdirect 口径统计——
    分子 = is_negative 且 predicted_boundary==auto_suggest 的样本数；
    分母 = is_negative 样本总数。覆盖全部语义高风险类别，与 run_vpn_eval 的 auto_misdirect 互补。
    """
    negative = [r for r in records if r.is_negative]
    neg_total = len(negative)
    misdirected = [r for r in negative if r.predicted_boundary == BOUNDARY_AUTO_SUGGEST]
    return {
        "misdirect_rate": _rate(len(misdirected), neg_total) if neg_total else None,
        "misdirect_count": len(misdirected),
        "negative_sample_count": neg_total,
        "samples": [
            {
                "index": r.index,
                "scenario": r.scenario,
                "text": r.text,
                "predicted_boundary": r.predicted_boundary,
            }
            for r in misdirected
        ],
        "basis": (
            "定义：负向/越权(is_negative)样本被判定为 auto_suggest 即误放行；"
            "覆盖 ACL 越权/账号锁定/无知识答案/敏感高影响等语义高风险类别，不限敏感词命中；"
            "理想值 0，>0 需人工复核（与 run_vpn_eval 的 auto_misdirect 口径互补）"
        ),
    }


def compute_acl_rejection(records: list[VpnEvalRecord]) -> AclRejectionMetrics:
    """D3 ACL 越权拒绝率：ACL 越权样本被判 must_escalate 的比例（理想 1.0）。

    识别：scenario == acl_out_of_scope（v2 越权场景）。配合 run_vpn_eval 的空 expected 依据、
    repository 的 tenant+visibility+department ACL 过滤与 tool_governance 越权拒绝，
    断言越权样本必须被升级/拒绝，不得给任何自动建议。
    """
    acl = [r for r in records if r.scenario == SCENARIO_ACL]
    total = len(acl)
    rejected = sum(1 for r in acl if r.predicted_boundary == BOUNDARY_MUST_ESCALATE)
    non_rejected = [r for r in acl if r.predicted_boundary != BOUNDARY_MUST_ESCALATE]
    return {
        "rejection_rate": _rate(rejected, total) if total else None,
        "rejected_count": rejected,
        "acl_sample_count": total,
        "samples": [
            {
                "index": r.index,
                "text": r.text,
                "predicted_boundary": r.predicted_boundary,
            }
            for r in non_rejected
        ],
        "basis": (
            "ACL 越权样本(scenario=acl_out_of_scope)被判 must_escalate 即正确拒绝；"
            "理想 1.0（越权必须拒绝/升级），<1.0 的样本列入 samples 需人工复核与闭环"
        ),
    }


def compute_tool_failure(records: list[VpnEvalRecord]) -> ToolFailureMetrics:
    """M8 工具调用失败率：失败次数/总次数（denied/timeout/failed/error/cancelled 视为失败）。"""
    total = 0
    failed = 0
    for r in records:
        for status in r.tool_call_statuses:
            total += 1
            if status in FAILED_TOOL_STATUSES:
                failed += 1
    return {
        "failure_rate": _rate(failed, total) if total else None,
        "failed_count": failed,
        "total_count": total,
        "basis": (
            "从 tool_trace / tool_governance 状态计数：denied/timeout/failed/error/cancelled 视为失败，"
            "completed 视为成功；无工具调用记录时返回 None"
        ),
    }


def compute_latency(records: list[VpnEvalRecord]) -> LatencyMetrics:
    """M9a P95 延迟：取全套样本（目前为分类器延迟；诊断 E2E 应重定义采集点为完整链路）。"""
    values = sorted(r.latency_ms for r in records if r.latency_ms is not None)
    return {
        "p50": round(_quantile(values, 0.50), 3),
        "p95": round(_quantile(values, 0.95), 3),
        "sample_count": len(values),
    }


def compute_cost(
    records: list[VpnEvalRecord],
    *,
    input_per_1k: float = DEFAULT_INPUT_PER_1K_USD,
    output_per_1k: float = DEFAULT_OUTPUT_PER_1K_USD,
) -> CostMetrics:
    """M9b 单工单成本 = Σ(model input/output tokens × 单价)/工单数。

    单价为 0 时标记 rated=False（unrated），per_ticket_usd=None，不抛异常。
    """
    total_input = sum(r.input_tokens for r in records)
    total_output = sum(r.output_tokens for r in records)
    ticket_count = len(records)
    total_usd = round(
        (total_input / 1000.0) * input_per_1k + (total_output / 1000.0) * output_per_1k, 8
    )
    rated = input_per_1k > 0 or output_per_1k > 0
    per_ticket = round(total_usd / ticket_count, 8) if (ticket_count and rated) else None
    return {
        "per_ticket_usd": per_ticket,
        "total_usd": total_usd,
        "ticket_count": ticket_count,
        "input_per_1k_usd": input_per_1k,
        "output_per_1k_usd": output_per_1k,
        "rated": rated,
        "basis": (
            "单工单成本 = Σ(input/output token × 单价)/工单数；单价来自 MODEL_INPUT/OUTPUT_COST_PER_1K_USD；"
            "单价为 0 时标记 unrated(per_ticket_usd=None) 不抛异常；VPN 诊断 token 需在 agent.run 内埋点纳入计量"
        ),
    }


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------


def compute_metrics_report(
    records: list[VpnEvalRecord],
    *,
    mode: str = KnowledgeMode.STATIC.value,
    input_per_1k: float = DEFAULT_INPUT_PER_1K_USD,
    output_per_1k: float = DEFAULT_OUTPUT_PER_1K_USD,
) -> VpnMetricsReport:
    """计算全套指标（M1-M9 + 阶段三证据链闭环指标），供 run_vpn_eval.py 写入报告。"""
    return {
        "vpn_fault_classification": compute_classification(records),
        "field_completion": compute_field_completion(records),
        "fault_hypothesis_hit": compute_hypothesis_hit(records),
        "fault_hypothesis_accuracy": compute_fault_hypothesis_accuracy(records),
        "escalation_accuracy": compute_escalation(records),
        "customer_steps": compute_customer_steps(records),
        "reference_support": compute_reference_support(records, mode),
        "high_risk_misdirect": compute_high_risk_misdirect(records),
        "acl_rejection": compute_acl_rejection(records),
        "tool_failure": compute_tool_failure(records),
        "latency": compute_latency(records),
        "cost": compute_cost(records, input_per_1k=input_per_1k, output_per_1k=output_per_1k),
        "evidence_sufficiency": compute_evidence_sufficiency(records),
        "wrong_escalation": compute_wrong_escalation(records),
        "customer_step_completion": compute_customer_step_completion(records),
        "rediagnosis_success": compute_rediagnosis_success(records),
        "manual_takeover": compute_manual_takeover(records),
    }


# ---------------------------------------------------------------------------
# 与 run_vpn_eval 的桥接：把每条 case 评估结果规范化为 VpnEvalRecord
# ---------------------------------------------------------------------------


def to_record(case_result: Mapping[str, Any]) -> VpnEvalRecord:
    """把 run_vpn_eval._evaluate_case 产出的字典规范化为 VpnEvalRecord。

    - M3 假设：无结构化假设时用 expected_fault / predicted_fault 回退（compute_hypothesis_hit 内处理）；
    - M4/M5/M8 诊断相关字段默认缺省，由 `from_diagnosis` 扩展注入；
    - M9 成本 token 默认 0（静态评估不跑模型）。
    """
    return VpnEvalRecord(
        index=case_result.get("index"),
        scenario=str(case_result.get("scenario") or ""),
        text=str(case_result.get("text") or ""),
        is_negative=bool(case_result.get("is_negative", False)),
        expected_fault=str(case_result.get("vpn_fault", "")),
        predicted_fault=str(case_result.get("predicted_fault", "")),
        expected_missing=tuple(case_result.get("expected_missing") or ()),
        actual_missing=tuple(case_result.get("actual_missing") or ()),
        expected_boundary=str(case_result.get("expected_boundary", "")),
        predicted_boundary=str(case_result.get("predicted_boundary", "")),
        expected_document_ids=tuple(case_result.get("expected_document_ids") or ()),
        retrieved_document_ids=tuple(case_result.get("retrieved_document_ids") or ()),
        reference_supported=case_result.get("reference_supported"),
        has_evidence=bool(case_result.get("has_evidence", False)),
        is_high_risk=bool(case_result.get("has_sensitive_risk", False))
        or bool(case_result.get("has_high_impact", False)),
        misdirect=bool(case_result.get("misdirect", False)),
        latency_ms=case_result.get("latency_ms"),
        # D3：v2 新增指标字段透传（无则 None/空，metrics 消费可直接读取）。
        error_code=case_result.get("error_code"),
        client_version=case_result.get("client_version"),
        network_type=case_result.get("network_type"),
        fault_hypothesis=str(case_result.get("fault_hypothesis") or ""),
        acls=tuple(case_result.get("acls") or ()),
        risk_level=str(case_result.get("risk_level") or ""),
        escalation_expected=case_result.get("escalation_expected"),
        departments=tuple(case_result.get("departments") or ()),
        internal=bool(case_result.get("internal", False)),
        resource=str(case_result.get("resource") or ""),
        # 阶段三：结构化证据链假设 + 证据充分率（M3 真实口径）。
        expected_hypothesis_code=case_result.get("expected_hypothesis_code"),
        evidence_hypothesis=case_result.get("evidence_hypothesis"),
        hypothesis_confidence=case_result.get("hypothesis_confidence"),
        evidence_found=bool(case_result.get("evidence_found", False)),
        # M5 真实口径：agent.run 轮次/工具数。
        agent_rounds=case_result.get("agent_rounds"),
        agent_tool_call_count=case_result.get("agent_tool_call_count"),
        # 阶段三新增闭环指标字段（无则 None/False，metrics 消费可直接读取）。
        wrong_escalation=bool(case_result.get("wrong_escalation", False)),
        customer_steps_completed=case_result.get("customer_steps_completed"),
        is_rediagnosis=bool(case_result.get("is_rediagnosis", False)),
        rediagnosis_success=case_result.get("rediagnosis_success"),
        manual_takeover=case_result.get("manual_takeover"),
    )


def records_from_case_results(
    results: Sequence[Mapping[str, Any]],
) -> list[VpnEvalRecord]:
    """批量规范化 case 评估结果。"""
    return [to_record(item) for item in results]


# ---------------------------------------------------------------------------
# 阶段三：与阶段二（t2 客户处置闭环）结构化的取数桥接
# ---------------------------------------------------------------------------
# 以下函数把 backend/vpn/closed_loop.VpnClosedLoopService.get_snapshot() 产出的
# 结构化快照（已 model_dump(mode="json") 的 dict）归一化为 VpnEvalRecord，
# 供「需要真实诊断 run / 客户动作数据才能算」的阶段三指标（customer_step_completion /
# rediagnosis_success / manual_takeover / M3 真实口径）消费。
#
# 口径说明（需求方 = metrics）：
#   - customer_step_completed : 该工单是否产出 provide_steps 且客户回填结果(结果非空)。
#   - is_rediagnosis          : 该工单诊断运行数 >= 2（存在再次诊断）。
#   - rediagnosis_success     : 再次诊断是否落定为 completed/handed_off（非 failed/cancelled）。
#   - manual_takeover         : 最近一次 run 是否转人工(next_action=escalate_incident/
#                              request_approval 或 run.status=handed_off/failed 或 must_handoff)。
#   - evidence_hypothesis / hypothesis_confidence / evidence_found : 最近一次 run 的
#                              hypothesis(结构化假设编码)/confidence/evidence 非空。
#
# 取数基于纯 dict 快照（duck-typing，不 import t2 对象类型），因此即便 t2 尚未落地
# 也能独立单测；t2 落地后只需把 get_snapshot() 的结果传入即可对齐。以下字段若快照缺失，
# 一律按「不适用」回退（None/False），不抛异常。


def _snapshot_runs(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """从快照提取 runs 列表（兼容 runs 或 latest_run 形态）。"""
    runs = list(snapshot.get("runs") or [])
    if not runs and snapshot.get("latest_run"):
        runs = [snapshot["latest_run"]]
    return [r for r in runs if isinstance(r, Mapping)]


def _run_next_action(run: Mapping[str, Any]) -> str:
    return str(run.get("next_action") or "")


def _run_status(run: Mapping[str, Any]) -> str:
    return str(run.get("status") or "")


def _snapshot_has_customer_result(snapshot: Mapping[str, Any]) -> bool:
    """快照中客户动作结果是否非空（存在已回填的 VpnCustomerActionResult）。"""
    results = list(snapshot.get("results") or [])
    return bool(results)


def _snapshot_action_count(snapshot: Mapping[str, Any]) -> int:
    """快照中产出的客户排查步骤数（provide_steps 的 VpnCustomerAction 数）。"""
    return len(list(snapshot.get("actions") or []))


def _snapshot_is_rediagnosis(snapshot: Mapping[str, Any]) -> bool:
    """该工单是否发生过再次诊断（诊断运行数 >= 2）。"""
    return len(_snapshot_runs(snapshot)) >= 2


def _snapshot_rediagnosis_success(snapshot: Mapping[str, Any]) -> bool | None:
    """再次诊断是否成功落定。

    有再次诊断（>=2 次 run）时：最近一次 run 状态为 completed/handed_off 即成功，
    failed/cancelled 即失败；不足 2 次 run 或无最近 run -> None（不适用）。
    """
    if not _snapshot_is_rediagnosis(snapshot):
        return None
    latest = _snapshot_runs(snapshot)[-1]
    status = _run_status(latest)
    if status in ("completed", "handed_off"):
        return True
    if status in ("failed", "cancelled"):
        return False
    return None


def _snapshot_manual_takeover(snapshot: Mapping[str, Any]) -> bool | None:
    """该工单最近一次诊断是否转人工接管。

    run 的 next_action 为 escalate_incident/request_approval，或 run.status 为
    handed_off/failed，或该 run 标记 must_handoff（reason_codes 或 evaluation）。
    无 run -> None（不适用）。
    """
    runs = _snapshot_runs(snapshot)
    if not runs:
        return None
    latest = runs[-1]
    action = _run_next_action(latest)
    status = _run_status(latest)
    reason_codes = [str(c) for c in (latest.get("reason_codes") or [])]
    if action in ESCALATION_COMMANDS or action == "assign_agent":
        return True
    if status in ("handed_off", "failed"):
        return True
    if any("handoff" in code or "escalate" in code for code in reason_codes):
        return True
    if latest.get("must_handoff") is True:
        return True
    # 未显式转人工（有 provide_steps / ask_customer 等非转人工命令）
    return False


def _snapshot_hypothesis(snapshot: Mapping[str, Any]) -> tuple[str | None, float | None, bool]:
    """从最近一次 run 提取结构化假设（hypothesis / confidence / evidence_found）。

    返回 (evidence_hypothesis, hypothesis_confidence, evidence_found)。
    """
    runs = _snapshot_runs(snapshot)
    if not runs:
        return None, None, False
    latest = runs[-1]
    hypothesis = latest.get("hypothesis")
    if not hypothesis:
        hypothesis = None
    confidence = latest.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    evidence = latest.get("evidence") or []
    evidence_found = (
        bool(evidence)
        or bool(latest.get("must_handoff")) is False
        and bool(latest.get("reason_codes") or [])
    )
    return hypothesis, confidence, bool(evidence_found)


def to_record_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    index: int | None = None,
    is_negative: bool = False,
    expected_hypothesis_code: str | None = None,
    text: str = "",
) -> VpnEvalRecord:
    """把 t2 诊断闭环快照归一化为 VpnEvalRecord（阶段三取数桥接）。

    只填充阶段三指标真实口径所需的诊断运行/客户动作字段；分类/字段/边界等静态字段
    由调用方（evaluate_case / to_record）另行提供，本函数不重复计算。

    口径：见本段 docstring。快照缺失字段一律回退 None/False，不抛异常。
    """
    runs = _snapshot_runs(snapshot)
    action_count = _snapshot_action_count(snapshot)
    has_result = _snapshot_has_customer_result(snapshot)
    evidence_hypothesis, hypothesis_confidence, evidence_found = _snapshot_hypothesis(snapshot)

    return VpnEvalRecord(
        index=index,
        is_negative=is_negative,
        text=text,
        expected_hypothesis_code=expected_hypothesis_code,
        evidence_hypothesis=evidence_hypothesis,
        hypothesis_confidence=hypothesis_confidence,
        evidence_found=evidence_found,
        # M5 真实口径：agent.run 轮次/工具数（快照未提供则 None，交由调用方补）。
        agent_tool_call_count=len(runs),
        # 客户步骤完成率：产出步骤且有客户回填结果为完成。
        customer_steps_completed=(has_result if action_count > 0 else None),
        # 再次诊断成功率。
        is_rediagnosis=_snapshot_is_rediagnosis(snapshot),
        rediagnosis_success=_snapshot_rediagnosis_success(snapshot),
        # 平均人工接管率。
        manual_takeover=_snapshot_manual_takeover(snapshot),
        # M4 升级口径：由最近 run 的命令标注（若快照带 produced_command）。
        produced_command=_run_next_action(runs[-1]) if runs else None,
        diagnosis_must_handoff=(_snapshot_manual_takeover(snapshot) is True),
    )


def records_from_diagnosis_snapshots(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    is_negative: bool = False,
    expected_hypothesis_codes: Sequence[str | None] | None = None,
) -> list[VpnEvalRecord]:
    """批量规范化 t2 诊断闭环快照列表 → VpnEvalRecord 列表（阶段三取数通道）。"""
    codes = list(expected_hypothesis_codes or [None] * len(snapshots))
    return [
        to_record_from_snapshot(
            snap,
            index=idx,
            is_negative=is_negative,
            expected_hypothesis_code=(codes[idx] if idx < len(codes) else None),
        )
        for idx, snap in enumerate(snapshots)
    ]
