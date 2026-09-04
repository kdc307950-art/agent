"""LANGGraph VPN Diagnosis Agent — 结构化数据契约（模型/枚举/Handoff 判定）。

模块归属：backend/vpn（本任务为「在既有确定性状态机 helpdesk 受理图 / vpn-v1 之上，
新增一个只读的 VPN Diagnosis Agent」的契约层）。

设计要点（对齐 docs/product/vpn-diagnosis-agent-contract.md）：
    - DiagnosisCommandType：本 agent 被允许产出的 5 个处置命令（只读诊断，不代发/不改单）。
    - FORBIDDEN_COMMANDS：7 个永远禁止产出的命令（账号/权限/网关/关单/代发消息等高风险副作用）。
    - DiagnosisCommand：extra="forbid" + model_validator 拒绝禁止命令 —— 模型文本无法直出业务命令，
      与 backend/copilot/models.py 的 extra="forbid" / Literal[False] 模式一致。
    - HandoffReason / DiagnosisEvaluation / evaluate_handoff：确定性四类「必须转人工」判定，
      顺序即优先级：multi_user_impact > identity_missing > no_evidence > low_confidence；
      比 boundary_vpn 的 must_escalate 更严（额外把身份缺失与低置信度显式升级为独立人工条件）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DiagnosisCommandType(StrEnum):
    """本 agent 允许产出的处置命令（5 项，全部无副作用）。

    - ask_customer       : 向客户追问缺失信息（→ 工单 AWAITING_CUSTOMER）
    - provide_steps      : 提供排查步骤草稿（仅草稿，不直接发消息 → ANSWER_PROPOSED）
    - assign_agent       : 派单给坐席（→ ASSIGNED）
    - escalate_incident  : 升级为事件/转人工队列（不回复 → QUEUED）
    - request_approval   : 请求审批（→ AWAITING_APPROVAL）
    """

    ASK_CUSTOMER = "ask_customer"
    PROVIDE_STEPS = "provide_steps"
    ASSIGN_AGENT = "assign_agent"
    ESCALATE_INCIDENT = "escalate_incident"
    REQUEST_APPROVAL = "request_approval"


# 允许命令集合（executor 集合校验用）：与 DiagnosisCommandType 的 5 个值等价。
ALLOWED_COMMANDS: frozenset[str] = frozenset(command.value for command in DiagnosisCommandType)


class HandoffReason(StrEnum):
    """必须转人工的具体原因（确定性编码，审计/展示用）。"""

    NO_EVIDENCE = "no_evidence"              # 证据不足：工具未返回可支撑结论的依据
    LOW_CONFIDENCE = "low_confidence"        # 置信度低于阈值（默认对齐 0.80）
    MULTI_USER_IMPACT = "multi_user_impact"  # 群体/多用户影响（对应 vpn_fault=multi_user_impact）
    IDENTITY_MISSING = "identity_missing"    # 身份缺失：无法确认用户/资产/账号归属


# 必须转人工的默认置信度阈值（对齐 backend/copilot/service.py 的 MIN_CONFIDENCE=0.80）。
DEFAULT_HANDOFF_CONFIDENCE = 0.80


class DiagnosisCommand(BaseModel):
    """单条结构化处置命令；extra="forbid" 禁止自由字段，杜绝模型文本直出业务命令。

    字段：
        command      : 只能是 DiagnosisCommandType 的 5 个允许值
        content      : 面向坐席/客户的命令文本（解释性，非业务字段）
        payload      : 命令参数（如 assign_agent 的目标 team_id / escalate 的事件信息）
        reason_codes : 决策依据编码（审计用）
        confidence   : 0..1
    """

    model_config = ConfigDict(extra="forbid")

    command: DiagnosisCommandType
    content: str = Field(default="", max_length=8_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _reject_forbidden(self) -> DiagnosisCommand:
        """防御性拒绝：command 落在禁止集合或非允许集合内直接抛 ValueError。

        正常情形下 command 已被枚举约束为合法值；此处为 executor 兜底之外的
        第二道防线，确保任何入口都无法构造出副作用命令。
        """
        if str(self.command) in FORBIDDEN_COMMANDS:
            raise ValueError(f"禁止命令，不可由 VPN Diagnosis Agent 产出: {self.command}")
        return self


class DiagnosisRequest(BaseModel):
    """VPN 诊断请求：工单上下文的不可变快照（由 service 层组装）。

    只携带诊断所需的只读上下文；租户/坐席身份由 RunContext 提供，不进入本模型。
    fault 为 vpn_fault（对齐 VPN_FAULT_* 枚举值）；identity_ok 由 service 层根据
    RunContext 的 tenant_id / user_id 判定（身份缺失时转人工），供 agent 侧 handoff 判定。
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    requester_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    ticket_text: str = Field(default="", max_length=8_000)
    fault: str = Field(default="connection_failed", max_length=64)
    asset_id: str | None = Field(default=None, max_length=64)
    current_status: str = Field(default="", max_length=32)
    identity_ok: bool = True


class DiagnosisEvaluation(BaseModel):
    """evaluate_handoff 的判定结果。

    must_handoff  : 是否必须转人工（四类任意命中即 True）
    handoff_reasons: 命中的 HandoffReason 列表（空代表不转人工）
    evidence_found : 是否找到可支撑结论的依据（no_evidence 判定的输入，留痕用）
    confidence     : 落定的置信度（留痕用）
    """

    model_config = ConfigDict(extra="forbid")

    must_handoff: bool
    handoff_reasons: list[HandoffReason] = Field(default_factory=list, max_length=16)
    evidence_found: bool = False
    confidence: float = Field(ge=0.0, le=1.0)


# 本模块导入时须可见（见 __init__.py 导出）；在此定义避免循环依赖。
FORBIDDEN_COMMANDS: frozenset[str] = frozenset(
    {
        "reset_password",
        "unlock_account",
        "grant_vpn_permission",
        "modify_vpn_config",
        "restart_gateway",
        "close_ticket",
        "send_customer_message",
    }
)


def _has_evidence(evidence: Any) -> bool:
    """判定工具是否返回了可支撑结论的依据（no_evidence 的输入）。

    兼容 list/tuple/dict/对象：空容器、None、空串均视为无证据；
    带 found:false 标记的字典结果视为无证据。
    """
    if evidence is None:
        return False
    if isinstance(evidence, (list, tuple)):
        return any(_has_evidence(item) for item in evidence)
    if isinstance(evidence, dict):
        if evidence.get("found") is False:
            return False
        # 去掉 content 展示文本后仍有业务字段才算证据
        return bool({k: v for k, v in evidence.items() if k not in ("content",)})
    if isinstance(evidence, str):
        return bool(evidence.strip())
    return bool(evidence)


def evaluate_handoff(
    *,
    evidence: Any,
    fault: str,
    confidence: float,
    identity_ok: bool,
    min_confidence: float = DEFAULT_HANDOFF_CONFIDENCE,
) -> DiagnosisEvaluation:
    """四类「必须转人工」的确定性判定（纯函数，无 IO，可单测）。

    判定顺序即优先级（每命中一项追加 reason）：
        1. multi_user_impact : fault == "multi_user_impact"
        2. identity_missing  : identity_ok is False（RunContext 无 tenant_id / user_id）
        3. no_evidence       : 工具未返回任何可支撑结论的依据
        4. low_confidence    : confidence < min_confidence（默认 0.80，对齐 copilot）

    返回 DiagnosisEvaluation；其余情况 must_handoff=False，handoff_reasons=[]。
    与 boundary_vpn 的关系：multi_user_impact 与 no_evidence 对应其 must_escalate，
    但本判定额外把 identity_missing 与 low_confidence 显式独立为人工条件（更严）。
    """
    confidence = max(0.0, min(1.0, float(confidence)))
    evidence_found = _has_evidence(evidence)
    reasons: list[HandoffReason] = []

    if fault == "multi_user_impact":
        reasons.append(HandoffReason.MULTI_USER_IMPACT)
    if not identity_ok:
        reasons.append(HandoffReason.IDENTITY_MISSING)
    if not evidence_found:
        reasons.append(HandoffReason.NO_EVIDENCE)
    if confidence < float(min_confidence):
        reasons.append(HandoffReason.LOW_CONFIDENCE)

    return DiagnosisEvaluation(
        must_handoff=bool(reasons),
        handoff_reasons=reasons,
        evidence_found=evidence_found,
        confidence=round(confidence, 4),
    )
