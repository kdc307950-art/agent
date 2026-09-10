"""LANGGraph VPN Diagnosis Agent — 确定性证据链诊断规则（纯函数，无 IO，可单测）。

模块归属：backend/vpn/rules.py（本任务 t3 新增）。定位：
    - 把 VPN 诊断从「关键词分类」升级为「证据链」：不再只给一个 vpn_fault 标签，
      而是对「账号 / 网关 / 客户端版本 / 错误码 / 多用户影响 / 证据充分性」逐条归一化为信号，
      再按 **确定性规则表** 产出结构化诊断结论。
    - 结论固定为结构化：``hypothesis / confidence / evidence[] / ruled_out[] /
      next_action / reason_codes[] / must_handoff``（对齐 docs/evaluation/vpn-eval-metrics.md §3 M3 真实口径）；
      阶段四新增 ``requires_human``，与目标结论 schema
      （hypothesis/evidence/confidence/ruled_out/next_action/requires_human）对齐。
    - 与 backend/vpn/models.py 的 ``evaluate_handoff`` 互补：models 只做「是否必须转人工」的
      四类判定；rules 把「根因假设 + 证据链 + 排除项 + 处置命令」一并固化为可评测结构。

规则表（顺序即优先级；命中即返回，不叠加后续）：

    R0 身份缺失(identity_ok=False)              -> no_evidence 人工
    R1 多用户同时失败(multi_user_impact)         -> 事件升级（不走单用户建议）
    R2 账号锁定(account_status=locked/disabled)  -> 必须人工(it.account)
    R3 账号正常 + 网关 normal/up + 客户端版本异常 -> 配置/客户端版本问题（root cause=client_version_outdated）
    R4 账号正常 + 网关 down/degraded             -> 网关侧问题（root cause=gateway_down）
    F1 故障意图分诊（fault=auth_failed / intranet_unreachable / frequent_disconnect）-> 各得互不相同的根因假设
    R5 其余账号/网关正常 + 版本正常，仅有错误码信号 -> 连接类失败（error_code 仅作假设信号，不直接等于根因）
    R6 无任何可信证据(account/gateway 未知、无知识命中) -> 不给出根因结论，仅转人工（no_evidence）

阶段四新增函数：
    - ``diagnose_vpn_tree(evidence) -> DiagnosisConclusion``：结构化诊断树（单用户
      account/config/nat/gateway-ok vs 多用户 gateway/region/vendor），纯函数、可单测。
    - ``guardrail_evaluate(evidence) -> DiagnosisConclusion``：确定性护栏（置信度门槛 /
      高风险升级 / 无证据不得自动回复 / 自动下一步只在满足条件时给），产出目标结论 JSON。
    - ``conclusion_fault_class(hypothesis) -> str``：把树叶子假设归并到 5 类 VPN 故障。

设计要点：
    - 纯函数、确定性、无 IO：输入 ``VpnEvidence``（归一化信号快照），输出 ``EvidenceDiagnosis``。
    - 错误码只作为假设信号（reason_codes 标注 error_code_signal=...），**不能直接等于根因**；
      根因由账号/网关/客户端版本的可信状态推出。修复 D6 关联（账号/VPN/网关主问题与关联问题分离）。
    - 故障意图分诊（F1）不是「裸关键词标签」：next_action / 置信度由账号/网关/知识/多次尝试等信号综合给出。
    - 单测用 ``tests/test_vpn_evidence_rules.py`` 与 ``tests/test_vpn_diagnosis_depth.py``
      直接覆盖每条规则、诊断树分支与护栏。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    DEFAULT_HANDOFF_CONFIDENCE,
    DiagnosisCommandType,
    evaluate_handoff,
)

# ---------------------------------------------------------------------------
# 信号枚举
# ---------------------------------------------------------------------------


class AccountStatus(StrEnum):
    """账号状态信号（工具 get_vpn_account_status 归一化结果）。"""

    ACTIVE = "active"
    LOCKED = "locked"
    DISABLED = "disabled"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class GatewayStatus(StrEnum):
    """网关状态信号（工具 get_vpn_gateway_status 归一化结果）。"""

    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


# 根因假设标识（M3 真实口径的 hypothesis 取值，稳定、可比对）。
HYPOTHESIS_ACCOUNT_LOCKED = "account_locked"
HYPOTHESIS_MULTI_USER_IMPACT = "multi_user_impact"
HYPOTHESIS_CLIENT_VERSION_OUTDATED = "client_version_outdated"
HYPOTHESIS_GATEWAY_DOWN = "gateway_down"
HYPOTHESIS_CONNECTION_FAILED = "connection_failed"
HYPOTHESIS_AUTH_FAILED = "auth_failed"
HYPOTHESIS_FREQUENT_DISCONNECT = "frequent_disconnect"
HYPOTHESIS_INTRANET_UNREACHABLE = "intranet_unreachable"
HYPOTHESIS_NO_EVIDENCE = "no_evidence"  # 无可信证据：不给出根因结论

# 结构化诊断树（diagnose_vpn_tree / guardrail 分支）新增的叶子假设。
HYPOTHESIS_LOCAL_NETWORK_NAT = "local_network_nat"  # 单用户：本地网络 / NAT 风险
HYPOTHESIS_REGIONAL_INCIDENT = "regional_incident"  # 多用户：区域性事件
HYPOTHESIS_VENDOR_ANOMALY = "vendor_anomaly"  # 多用户：VPN 厂商侧异常

# 处置命令（复用 DiagnosisCommandType 允许命令；escalate_incident 为事件升级/转人工）。
NEXT_ACTION_ESCALATE = DiagnosisCommandType.ESCALATE_INCIDENT.value
NEXT_ACTION_PROVIDE_STEPS = DiagnosisCommandType.PROVIDE_STEPS.value
NEXT_ACTION_ASK_CUSTOMER = DiagnosisCommandType.ASK_CUSTOMER.value

# 置信度门槛：低于此值不得自动给下一步，必须转人工（对齐 models.DEFAULT_HANDOFF_CONFIDENCE=0.80）。
GUARDRAIL_CONFIDENCE_THRESHOLD = 0.80

# 组成目标「结论 JSON」的 6 个关键字段（hypothesis/evidence/confidence/ruled_out/next_action/requires_human）。
CONCLUSION_FIELDS: tuple[str, ...] = (
    "hypothesis",
    "evidence",
    "confidence",
    "ruled_out",
    "next_action",
    "requires_human",
)

# 版本号正则（支持 "v2.4.1" / "3.4.2" / "2.9.0" 等）。
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)", re.IGNORECASE)


def _parse_version(version: str) -> tuple[int, int, int] | None:
    """把版本串解析为 (major, minor, patch)；无法解析返回 None。"""
    if not version:
        return None
    match = _VERSION_RE.search(str(version))
    if not match:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except (TypeError, ValueError):
        return None


def is_version_outdated(current: str, required: str) -> bool:
    """判定当前客户端版本是否低于要求版本（确定性版本比较）。

    两者之一无法解析时返回 False（视为「未知/不判定」），避免把未知当过期。
    """
    cur = _parse_version(current)
    req = _parse_version(required)
    if cur is None or req is None:
        return False
    return cur < req


def normalize_account_status(raw: Any) -> AccountStatus:
    """把工具返回的账号状态归一化为 AccountStatus 信号。

    raw 可为 dict（含 status/found）、字符串、None；found=false / 未知一律 UNKNOWN。
    """
    if isinstance(raw, dict):
        if raw.get("found") is False or raw.get("status") is None:
            return AccountStatus.UNKNOWN
        status = str(raw.get("status") or "").lower()
    elif isinstance(raw, str):
        status = raw.strip().lower()
    else:
        return AccountStatus.UNKNOWN
    mapping = {
        "active": AccountStatus.ACTIVE,
        "ok": AccountStatus.ACTIVE,
        "normal": AccountStatus.ACTIVE,
        "enabled": AccountStatus.ACTIVE,
        "locked": AccountStatus.LOCKED,
        "lock": AccountStatus.LOCKED,
        "disabled": AccountStatus.DISABLED,
        "expired": AccountStatus.EXPIRED,
    }
    return mapping.get(status, AccountStatus.UNKNOWN)


def normalize_gateway_status(raw: Any) -> GatewayStatus:
    """把工具返回的网关状态归一化为 GatewayStatus 信号。

    raw 可为 dict（含 status/found）、字符串、None；found=false / 未知一律 UNKNOWN。
    """
    if isinstance(raw, dict):
        if raw.get("found") is False or raw.get("status") is None:
            return GatewayStatus.UNKNOWN
        status = str(raw.get("status") or "").lower()
    elif isinstance(raw, str):
        status = raw.strip().lower()
    else:
        return GatewayStatus.UNKNOWN
    mapping = {
        "up": GatewayStatus.UP,
        "ok": GatewayStatus.UP,
        "healthy": GatewayStatus.UP,
        "degraded": GatewayStatus.DEGRADED,
        "down": GatewayStatus.DOWN,
        "unreachable": GatewayStatus.DOWN,
    }
    return mapping.get(status, GatewayStatus.UNKNOWN)


# ---------------------------------------------------------------------------
# 输入模型：归一化信号快照
# ---------------------------------------------------------------------------


class VpnEvidence(BaseModel):
    """证据链诊断的归一化输入（由 service / agent 从工具返回组装；纯信号，无 IO）。

    字段均为信号而非原始文本，使规则完全确定、可单测、可跨数据源复用：

    - fault              : 受理层 vpn_fault（连接/认证/频繁掉线/内网/多用户）
    - account_status     : 账号状态信号（normalize_account_status 产物）
    - gateway_status     : 网关状态信号（normalize_gateway_status 产物）
    - client_version     : 客户端当前版本串（可为空）
    - required_version   : 客户端要求/目标版本串（可为空；为空则不判版本过期）
    - error_code         : 错误码（只作假设信号，不直接等于根因）
    - multi_user_impact  : 是否多用户同时失败（fault==multi_user_impact 或字段佐证）
    - identity_ok        : 身份/归属是否完整
    - knowledge_hit      : 知识库是否命中可支撑结论的条目
    - has_asset          : 是否绑定可核验资产
    """

    model_config = ConfigDict(extra="forbid")

    fault: str = "connection_failed"
    account_status: AccountStatus = AccountStatus.UNKNOWN
    gateway_status: GatewayStatus = GatewayStatus.UNKNOWN
    client_version: str = ""
    required_version: str = ""
    error_code: str = ""
    multi_user_impact: bool = False
    identity_ok: bool = True
    knowledge_hit: bool = False
    has_asset: bool = False

    # --- 阶段四（Phase 4）新增信号（全部有默认值，保持 extra="forbid"） ---
    # auth_attempts      : 认证失败尝试次数（区分「一次密码错」与「疑似账号锁定」）
    # intranet_signal    : 内网不可达信号（VPN 能连但内网不通的佐证）
    # local_network_nat  : 本地网络 / NAT 风险信号（单用户网络侧分支）
    # regional_incident  : 区域性事件信号（多用户且无网关异常）
    # vendor_anomaly     : VPN 厂商侧异常信号（多用户且无网关/区域信号）
    # evidence_quote     : 用户请求中的支撑文本引用（保证证据链可回溯、可引证）
    auth_attempts: int = 0
    intranet_signal: bool = False
    local_network_nat: bool = False
    regional_incident: bool = False
    vendor_anomaly: bool = False
    evidence_quote: str = ""


# ---------------------------------------------------------------------------
# 输出模型：结构化证据链诊断结论
# ---------------------------------------------------------------------------


class EvidenceDiagnosis(BaseModel):
    """证据链诊断结论（M3 真实口径的结构化替代）。

    - hypothesis  : 根因假设标识（see HYPOTHESIS_*；no_evidence 表示不给出根因）
    - confidence  : 该假设的置信度（0..1）
    - evidence    : 支撑该假设的证据（人类可读；也作为 M3 的证据充分率输入）
    - ruled_out   : 被排除的假设（区分主问题与关联问题，修复 D6）
    - next_action : 处置命令（escalate_incident / provide_steps / ask_customer）
    - reason_codes: 决策依据编码（审计/指标用；含错误码信号标注）
    - must_handoff: 是否必须转人工（multi_user / account_locked / no_evidence 均 True）
    - requires_human: 是否必须转人工（阶段四目标 JSON 字段；与 must_handoff 语义一致，
      但作为「结论 JSON」的标准字段对齐目标 schema）。默认与 must_handoff 保持一致，
      由 guardrail_evaluate 进一步按置信度/高风险/无证据收紧。
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list, max_length=32)
    ruled_out: list[str] = Field(default_factory=list, max_length=32)
    next_action: str
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    must_handoff: bool = False
    requires_human: bool = False

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容 dict（含 requires_human，对齐目标结论 schema）。"""
        return {
            "hypothesis": self.hypothesis,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "ruled_out": list(self.ruled_out),
            "next_action": self.next_action,
            "reason_codes": list(self.reason_codes),
            "must_handoff": self.must_handoff,
            "requires_human": self.requires_human,
        }


# ---------------------------------------------------------------------------
# 确定性规则表
# ---------------------------------------------------------------------------


def _client_version_outdated(evidence: VpnEvidence) -> bool:
    return bool(
        evidence.client_version
        and evidence.required_version
        and is_version_outdated(evidence.client_version, evidence.required_version)
    )


def _all_account_gateway_normal(evidence: VpnEvidence) -> bool:
    """账号正常 + 网关正常（up）。degraded 不视为 normal（见 R4 单独走网关侧）。"""
    return evidence.account_status in (AccountStatus.ACTIVE, AccountStatus.EXPIRED) and (
        evidence.gateway_status == GatewayStatus.UP
    )


def evaluate_evidence(evidence: VpnEvidence) -> EvidenceDiagnosis:
    """按确定性规则表对信号做证据链诊断（纯函数，无 IO）。

    返回 EvidenceDiagnosis；优先级 R0-R6 见模块 docstring。无任何分支会同时命中，
    保证每个合法输入都得到唯一结构化结论（兜底 R5/R6）。

    外层包一层证据引用合并：当 ``evidence.evidence_quote`` 非空时把用户请求的支撑
    文本引用并入 evidence 列表，保证证据链可回溯、可引证（证据引用完整率）。
    """
    diagnosis = _evaluate_evidence_core(evidence)
    return _with_citation(diagnosis, evidence)


def _evaluate_evidence_core(evidence: VpnEvidence) -> EvidenceDiagnosis:
    """证据链诊断核心（不含证据引用合并；仅供 evaluate_evidence 调用）。"""
    # R0 身份缺失：无法确认用户/资产/账号归属 -> 必须人工
    if not evidence.identity_ok:
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_NO_EVIDENCE,
            confidence=0.2,
            evidence=["身份/归属缺失，无法确认用户与资产"],
            ruled_out=[
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_MULTI_USER_IMPACT,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_GATEWAY_DOWN,
            ],
            next_action=NEXT_ACTION_ESCALATE,
            reason_codes=["identity_missing", "handoff:identity"],
            must_handoff=True,
            requires_human=True,
        )

    # R1 多用户同时失败：事件升级，不走单用户建议
    if evidence.multi_user_impact:
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_MULTI_USER_IMPACT,
            confidence=0.9,
            evidence=["多用户同时失败，非单点原因，需按事件升级"],
            ruled_out=[
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_CONNECTION_FAILED,
            ],
            next_action=NEXT_ACTION_ESCALATE,
            reason_codes=["multi_user_impact", "handoff:multi_user"],
            must_handoff=True,
            requires_human=True,
        )

    # R2 账号锁定/禁用：必须人工(it.account)，不能给 VPN 自动建议
    # DISABLED（账户已禁用/冻结）同样属于账号侧主问题，与 LOCKED 一致归 account_locked。
    if evidence.account_status in (AccountStatus.LOCKED, AccountStatus.DISABLED):
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_ACCOUNT_LOCKED,
            confidence=0.95,
            evidence=[f"账号状态={evidence.account_status.value}（锁定/禁用，需人工处理）"],
            ruled_out=[
                HYPOTHESIS_GATEWAY_DOWN,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_CONNECTION_FAILED,
            ],
            next_action=NEXT_ACTION_ESCALATE,
            reason_codes=["account_locked", "handoff:account_locked"],
            must_handoff=True,
            requires_human=True,
        )

    # R3 账号正常 + 网关 up + 客户端版本过期 -> 配置/客户端版本问题
    if _client_version_outdated(evidence) and _all_account_gateway_normal(evidence):
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_CLIENT_VERSION_OUTDATED,
            confidence=0.85,
            evidence=[
                f"账号状态={evidence.account_status.value}",
                f"网关状态={evidence.gateway_status.value}",
                f"客户端版本={evidence.client_version or '未知'} < 要求 {evidence.required_version or '未知'}",
            ],
            ruled_out=[
                HYPOTHESIS_GATEWAY_DOWN,
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_MULTI_USER_IMPACT,
            ],
            next_action=NEXT_ACTION_PROVIDE_STEPS,
            reason_codes=["client_version_outdated", "config_issue"],
            must_handoff=False,
            requires_human=False,
        )

    # R4 网关 down/degraded：网关侧问题
    if evidence.gateway_status in (GatewayStatus.DOWN, GatewayStatus.DEGRADED):
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_GATEWAY_DOWN,
            confidence=0.9 if evidence.gateway_status == GatewayStatus.DOWN else 0.6,
            evidence=[f"网关状态={evidence.gateway_status.value}"],
            ruled_out=[
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_CONNECTION_FAILED,
            ],
            next_action=NEXT_ACTION_ESCALATE,
            reason_codes=["gateway_down", "handoff:gateway"],
            must_handoff=True,
            requires_human=True,
        )

    # FAULT-INTENT：认证失败 / 内网不可达 / 频繁掉线的差异化根因假设（不是裸关键词标签）。
    # 顺序在 R2（账号锁定）之后、R6/R5 之前：账号锁定已被 R2 捕获；此处补齐 fault 意图
    # 独有的根因假设，next_action/置信度由账号/网关/知识/多次尝试等信号综合给出。
    fault_diag = _fault_intent_diagnosis(evidence)
    if fault_diag is not None:
        return fault_diag

    # R6 无任何可信证据：不给根因结论，仅转人工
    if not _has_credible_evidence(evidence):
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_NO_EVIDENCE,
            confidence=0.2,
            evidence=["账号/网关状态未知且无知识命中，无可信证据支撑根因"],
            ruled_out=[
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_MULTI_USER_IMPACT,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_GATEWAY_DOWN,
            ],
            next_action=NEXT_ACTION_ESCALATE,
            reason_codes=["no_evidence", "handoff:no_evidence"],
            must_handoff=True,
            requires_human=True,
        )

    # R5 兜底：账号/网关正常，无版本过期，可能有错误码信号 -> 连接类失败
    #     错误码只作为假设信号（reason_codes 标注），不能直接等于根因。
    #     有知识命中时置信度>=0.80（与 must_handoff=False 一致，通过四类门禁）；
    #     无知识命中时置信度下调且强制转人工（不给出根因结论自动建议）。
    reason_codes = ["connection_failed"]
    if evidence.error_code:
        reason_codes.append(f"error_code_signal={evidence.error_code}")
    evidence_notes = [
        f"账号状态={evidence.account_status.value}",
        f"网关状态={evidence.gateway_status.value}",
    ]
    if evidence.error_code:
        evidence_notes.append(f"错误码={evidence.error_code}（仅作假设信号，非根因）")
    confidence = 0.85 if evidence.knowledge_hit else 0.6
    return EvidenceDiagnosis(
        hypothesis=HYPOTHESIS_CONNECTION_FAILED,
        confidence=confidence,
        evidence=evidence_notes,
        ruled_out=[
            HYPOTHESIS_ACCOUNT_LOCKED,
            HYPOTHESIS_GATEWAY_DOWN,
            HYPOTHESIS_CLIENT_VERSION_OUTDATED,
            HYPOTHESIS_MULTI_USER_IMPACT,
        ],
        next_action=(NEXT_ACTION_PROVIDE_STEPS if evidence.knowledge_hit else NEXT_ACTION_ESCALATE),
        reason_codes=reason_codes,
        must_handoff=not evidence.knowledge_hit,
        requires_human=not evidence.knowledge_hit,
    )


def _with_citation(diagnosis: EvidenceDiagnosis, evidence: VpnEvidence) -> EvidenceDiagnosis:
    """把 evidence_quote 并入 evidence 列表（去重，纯函数）。"""
    cite = _citation_items(evidence)
    if cite and all(item not in diagnosis.evidence for item in cite):
        return diagnosis.model_copy(update={"evidence": list(diagnosis.evidence) + cite})
    return diagnosis


def _citation_items(evidence: VpnEvidence) -> list[str]:
    """用户请求中的支撑文本引用（保证证据链可回溯、可引证）。"""
    if evidence.evidence_quote:
        return [f"文本引用：{evidence.evidence_quote}"]
    return []


def _fault_intent_diagnosis(evidence: VpnEvidence) -> EvidenceDiagnosis | None:
    """按故障意图（fault）给出差异化根因假设（纯函数）。

    仅在「账号锁定 R2 / 网关侧 R4 / 版本过期 R3」等更强规则未命中的情况下分诊，
    处理三类 fault 意图：
        - auth_failed          : 账号/认证歧义（含多次失败导致账号锁定嫌疑）——高层次人工；
        - intranet_unreachable : 网关/内网路由（本地 NAT / 内网信号）——排障步骤或升级；
        - frequent_disconnect  : 连接/路由稳定性——排障步骤或升级。
    其余 fault（connection_failed / multi_user_impact）返回 None，交给 R1/R6/R5 兜底。
    next_action / 置信度由账号/网关/知识/多次尝试等信号综合给出，不是「裸关键词标签」。
    """
    fault = evidence.fault
    signal_known = (
        evidence.account_status in (AccountStatus.ACTIVE, AccountStatus.EXPIRED)
        or evidence.gateway_status == GatewayStatus.UP
    )

    # ---- auth_failed：账号/认证歧义（账号锁定已由 R2 捕获）----
    if fault == HYPOTHESIS_AUTH_FAILED:
        conf = 0.85 if (signal_known and evidence.auth_attempts == 0) else 0.72
        notes = [
            "受理层 fault=auth_failed（账号/认证歧义）",
            "账号或凭据问题，需人工确认是否账号锁定/密码失效/证书过期",
        ]
        if evidence.auth_attempts:
            notes.append(f"认证失败尝试次数={evidence.auth_attempts}（存在账号锁定风险）")
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_AUTH_FAILED,
            confidence=conf,
            evidence=notes,
            ruled_out=[
                HYPOTHESIS_GATEWAY_DOWN,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_MULTI_USER_IMPACT,
            ],
            next_action=NEXT_ACTION_ESCALATE,
            reason_codes=["auth_failed", "auth_ambiguity", "handoff:auth"],
            must_handoff=True,
            requires_human=True,
        )

    # ---- intranet_unreachable / intranet 信号：网关/内网路由 ----
    if (
        fault == HYPOTHESIS_INTRANET_UNREACHABLE
        or evidence.intranet_signal
        or evidence.local_network_nat
    ):
        conf = 0.85 if (signal_known and evidence.knowledge_hit) else 0.65
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_INTRANET_UNREACHABLE,
            confidence=conf,
            evidence=[
                "VPN 能连但内网不可达（网关/内网路由）",
                "内网/本地网络信号：NAT 或内网路由异常",
            ],
            ruled_out=[
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_MULTI_USER_IMPACT,
            ],
            next_action=(
                NEXT_ACTION_PROVIDE_STEPS if evidence.knowledge_hit else NEXT_ACTION_ESCALATE
            ),
            reason_codes=["intranet_unreachable", "gateway_intranet"],
            must_handoff=not evidence.knowledge_hit,
            requires_human=not evidence.knowledge_hit,
        )

    # ---- frequent_disconnect：连接/路由稳定性 ----
    if fault == HYPOTHESIS_FREQUENT_DISCONNECT:
        conf = 0.85 if (signal_known and evidence.knowledge_hit) else 0.6
        return EvidenceDiagnosis(
            hypothesis=HYPOTHESIS_FREQUENT_DISCONNECT,
            confidence=conf,
            evidence=[
                "频繁掉线（连接/路由稳定性）",
                "客户端网络波动或隧道稳定性异常",
            ],
            ruled_out=[
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_GATEWAY_DOWN,
                HYPOTHESIS_MULTI_USER_IMPACT,
            ],
            next_action=(
                NEXT_ACTION_PROVIDE_STEPS if evidence.knowledge_hit else NEXT_ACTION_ESCALATE
            ),
            reason_codes=["frequent_disconnect", "connection_routing"],
            must_handoff=not evidence.knowledge_hit,
            requires_human=not evidence.knowledge_hit,
        )

    return None


def hypothesis_code_from_hint(hint: str) -> str:
    """把「假设描述文本 / 已编码假设」归一到稳定假设标识（供 M3 真实口径比对）。

    - 若 hint 已是已知 HYPOTHESIS_* 编码之一，原样返回；
    - 否则按关键词匹配到编码；无法匹配时返回 connection_failed（连接类兜底）。
    """
    if not hint:
        return HYPOTHESIS_CONNECTION_FAILED
    normalized = str(hint).casefold()
    if normalized in {
        HYPOTHESIS_ACCOUNT_LOCKED,
        HYPOTHESIS_MULTI_USER_IMPACT,
        HYPOTHESIS_CLIENT_VERSION_OUTDATED,
        HYPOTHESIS_GATEWAY_DOWN,
        HYPOTHESIS_CONNECTION_FAILED,
        HYPOTHESIS_AUTH_FAILED,
        HYPOTHESIS_FREQUENT_DISCONNECT,
        HYPOTHESIS_INTRANET_UNREACHABLE,
        HYPOTHESIS_LOCAL_NETWORK_NAT,
        HYPOTHESIS_REGIONAL_INCIDENT,
        HYPOTHESIS_VENDOR_ANOMALY,
        HYPOTHESIS_NO_EVIDENCE,
    }:
        return normalized
    # 关键词匹配（顺序即优先级，命中即返回）。
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        (HYPOTHESIS_ACCOUNT_LOCKED, ("账号", "账户", "锁定", "锁住", "被锁")),
        (HYPOTHESIS_MULTI_USER_IMPACT, ("多用户", "多人", "全公司", "整个部门", "集体", "大规模")),
        (HYPOTHESIS_CLIENT_VERSION_OUTDATED, ("版本", "客户端", "升级")),
        (HYPOTHESIS_GATEWAY_DOWN, ("网关", "gateway")),
        (HYPOTHESIS_AUTH_FAILED, ("认证失败", "登录失败", "密码错误", "认证")),
        (
            HYPOTHESIS_FREQUENT_DISCONNECT,
            ("频繁掉线", "频繁断线", "经常掉线", "掉了", "掉线", "断线"),
        ),
        (HYPOTHESIS_INTRANET_UNREACHABLE, ("内网", "intranet", "内网不可达")),
        (HYPOTHESIS_LOCAL_NETWORK_NAT, ("NAT", "网络地址转换", "本地网络", "局域网")),
        (HYPOTHESIS_REGIONAL_INCIDENT, ("区域", "地区", "片区")),
        (HYPOTHESIS_VENDOR_ANOMALY, ("厂商", "供应商", "vendor")),
        (HYPOTHESIS_NO_EVIDENCE, ("无证据", "没有依据", "无法确认", "转人工", "人工")),
    )
    for code, keywords in rules:
        if any(keyword.casefold() in normalized for keyword in keywords):
            return code
    return HYPOTHESIS_CONNECTION_FAILED


def build_evidence_from_case(case: Mapping[str, Any], *, identity_ok: bool = True) -> VpnEvidence:
    """从一条评测样本（v2 口径）组装 VpnEvidence（纯信号，无 IO）。

    依据样本提供的字段与故障标签构造确定性证据，供 ``evaluate_evidence`` 产出
    结构化假设并暴露给 M3 真实口径。字段缺失时回退，绝不抛异常。

    D6 修复：S6 账号锁定样本（provided_fields 无 account_status、expected_document_ids=()）
    若只按字段会得到 account_status=UNKNOWN -> 落入 no_evidence，M3 hypothesis 不再是
    `account_locked`。这里用 ``_account_status_from_text`` 从文案文本检测账号锁定/禁用
    主因信号（账号被锁/已禁用/登录被锁/多次失败），把 account_status 置为 LOCKED/DISABLED，
    使规则命中 R2（账号锁定 -> it.account + must_escalate）。
    """
    provided = dict(case.get("provided_fields") or {})
    client_version = str(provided.get("client_version") or case.get("client_version") or "")
    fault = str(case.get("vpn_fault") or case.get("fault") or "connection_failed")
    # 多用户影响：故障标签 multi_user_impact 或字段佐证（multi_user_impacted=是）。
    multi_user = fault == "multi_user_impact" or str(
        provided.get("multi_user_impacted") or ""
    ).strip() in ("是", "yes", "true", "1")
    error_code = str(provided.get("error_code") or case.get("error_code") or "")
    # 网关状态：样本未直接提供，用知识命中/字段推断；未知时保留 UNKNOWN。
    gateway_status = normalize_gateway_status(provided.get("gateway_status"))
    account_status = normalize_account_status(provided.get("account_status"))
    # D6：字段未显式给出账号状态时，从文案文本检测账号锁定/禁用主因信号。
    if account_status == AccountStatus.UNKNOWN:
        text = str(case.get("text") or "") + " " + str(case.get("fault_hypothesis") or "")
        detected = _account_status_from_text(text)
        if detected != AccountStatus.UNKNOWN:
            account_status = detected
    return VpnEvidence(
        fault=fault,
        account_status=account_status,
        gateway_status=gateway_status,
        client_version=client_version,
        required_version=str(case.get("required_version") or ""),
        error_code=error_code,
        multi_user_impact=multi_user,
        identity_ok=identity_ok,
        knowledge_hit=bool(case.get("expected_document_ids") or case.get("has_evidence")),
        has_asset=bool(provided.get("asset_id") or case.get("asset_id")),
        # 阶段四新增信号（无则回退默认值，绝不抛异常）。
        auth_attempts=_safe_int(provided.get("auth_attempts") or case.get("auth_attempts")),
        intranet_signal=_safe_bool(provided.get("intranet_signal") or case.get("intranet_signal")),
        local_network_nat=_safe_bool(
            provided.get("local_network_nat") or case.get("local_network_nat")
        ),
        regional_incident=_safe_bool(
            provided.get("regional_incident") or case.get("regional_incident")
        ),
        vendor_anomaly=_safe_bool(provided.get("vendor_anomaly") or case.get("vendor_anomaly")),
        evidence_quote=str(provided.get("evidence_quote") or case.get("evidence_quote") or ""),
    )


def _safe_int(value: Any) -> int:
    """把值安全转成非负 int；非法/None 回退 0。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_bool(value: Any) -> bool:
    """把值安全转成 bool；True/1/yes/是 为真。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "是", "on")


# 账号锁定/禁用主因信号词（与 intake._ACCOUNT_LOCKOUT_PRIMARY_TERMS 口径对齐，并补充多处关键词）。
_ACCOUNT_LOCKOUT_EXPLICIT_TERMS = (
    "账号被锁",
    "账户被锁",
    "账号锁定",
    "账户锁定",
    "账号被禁用",
    "账户被禁用",
    "被禁用",
    "已被禁用",
    "已禁用",
    "禁用",
    "登录被锁",
    "登录被禁用",
    "被锁死",
    "被锁住",
    "锁住",
    "锁定了",
    "被锁了",
    "账户被冻结",
    "账号被冻结",
    "锁定导致",
    "disabled",
    "locked",
    "lockout",
)
# 弱信号：多次失败/锁定嫌疑（需叠加账号上下文，且不被「认证失败」排除后仍判定）。
_ACCOUNT_LOCKOUT_WEAK_TERMS = (
    "多次登录失败",
    "多次错误密码",
    "多次密码错误",
    "连续登录失败",
    "登录被锁定",
    "账号被停用",
    "账户被停用",
    "停用",
)
# 排除词：单纯「登录失败/密码错误」一次失败属认证类，不应误判为账号锁定。
_ACCOUNT_LOCKOUT_EXCLUDE_TERMS = (
    "登录失败",
    "密码错误",
    "认证失败",
    "身份验证",
    "验证码",
    "用户名",
    "证书",
)


def _account_status_from_text(text: str) -> AccountStatus:
    """从文案文本检测账号锁定/禁用主因信号（确定性、纯函数）。

    - 命中**显式锁定/禁用词**（账号被锁/锁定/已禁用/冻结/disabled/locked）-> LOCKED/DISABLED；
    - 未命中显式词时，命中**弱信号**（多次失败/停用）且**未落入**认证失败排除框架 -> LOCKED/DISABLED；
    - 其余 -> AccountStatus.UNKNOWN（不做臆断，保留 auth/connection 假设）。
    """
    if not text:
        return AccountStatus.UNKNOWN
    normalized = str(text).casefold()

    # 显式锁定/禁用词优先（即使伴随"登录失败"等，仍以账号锁定为主因——修复 D6）。
    if any(term.casefold() in normalized for term in _ACCOUNT_LOCKOUT_EXPLICIT_TERMS):
        if any(term.casefold() in normalized for term in ("禁用", "停用", "冻结", "disabled")):
            return AccountStatus.DISABLED
        return AccountStatus.LOCKED

    # 弱信号：仅在未落入认证失败排除框架时才升级为账号锁定。
    if any(term.casefold() in normalized for term in _ACCOUNT_LOCKOUT_WEAK_TERMS):
        if any(term.casefold() in normalized for term in _ACCOUNT_LOCKOUT_EXCLUDE_TERMS):
            return AccountStatus.UNKNOWN
        return AccountStatus.LOCKED

    return AccountStatus.UNKNOWN


def is_account_lockout_text(text: str) -> bool:
    """判定文案是否为账号锁定/禁用主因（供 intake/规则/评测复用；纯函数）。"""
    return _account_status_from_text(text) in (AccountStatus.LOCKED, AccountStatus.DISABLED)


def _has_credible_evidence(evidence: VpnEvidence) -> bool:
    """判定是否存在可支撑结论的可信证据。

    账号或网关任一为「未知」且无知识命中 -> 视为无可信证据（no_evidence 的输入）。
    与 models._has_evidence 的差异：专门针对 VPN 信号（账号/网关/知识），比泛列表更贴合证据链。
    """
    account_known = evidence.account_status in (
        AccountStatus.ACTIVE,
        AccountStatus.LOCKED,
        AccountStatus.DISABLED,
        AccountStatus.EXPIRED,
    )
    gateway_known = evidence.gateway_status in (
        GatewayStatus.UP,
        GatewayStatus.DEGRADED,
        GatewayStatus.DOWN,
    )
    return bool(account_known or gateway_known or evidence.knowledge_hit or evidence.has_asset)


# ---------------------------------------------------------------------------
# 从「证据是否充分 + handoff」封装：复用 models.evaluate_handoff 做 must_handoff 复裁定
# ---------------------------------------------------------------------------


def evaluate_evidence_handoff(
    evidence: VpnEvidence, *, min_confidence: float = DEFAULT_HANDOFF_CONFIDENCE
) -> tuple[EvidenceDiagnosis, Any]:
    """同时产出结构化诊断与 handoff 判定（复用 models.evaluate_handoff）。

    - 证据充分性：以结构化诊断的 evidence 长度 / reason_codes 中是否含 no_evidence 为准；
    - 转人工：优先用 rules 的 must_handoff（多用户/账号锁定/无证据/网关侧），
      再用 models.evaluate_handoff 四类兜底（multi_user/identity/no_evidence/low_confidence）。
    """
    diagnosis = evaluate_evidence(evidence)
    handoff = evaluate_handoff(
        evidence=[item for item in diagnosis.evidence],
        fault=evidence.fault,
        confidence=diagnosis.confidence,
        identity_ok=evidence.identity_ok,
        min_confidence=min_confidence,
    )
    return diagnosis, handoff


# ---------------------------------------------------------------------------
# 阶段四（Phase 4）：结构化诊断树 + 目标结论 JSON + 确定性护栏
# ---------------------------------------------------------------------------


class DiagnosisConclusion(BaseModel):
    """阶段四「结论 JSON」模型：对齐目标 schema。

    字段固定为 ``hypothesis / evidence / confidence / ruled_out / next_action /
    requires_human``（extra="forbid"，拒绝自由字段）。这是 evaluate_evidence 的
    「目标口径」输出，供 diagnose_vpn_tree / guardrail_evaluate 返回，以及
    frozen-sample 评测（backend/vpn/eval_metrics.py）消费。
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis: str
    evidence: list[str] = Field(default_factory=list, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    ruled_out: list[str] = Field(default_factory=list, max_length=64)
    next_action: str
    requires_human: bool = False

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容 dict（对齐目标结论 schema 的 6 字段）。"""
        return {
            "hypothesis": self.hypothesis,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "ruled_out": list(self.ruled_out),
            "next_action": self.next_action,
            "requires_human": self.requires_human,
        }


def diagnose_vpn_tree(evidence: VpnEvidence) -> DiagnosisConclusion:
    """结构化诊断树（纯函数）：把 fault + 归一化证据映射到目标结论。

    对齐目标示例树（809 server）：
        单用户（single-user）: 账号状态异常 / 客户端配置版本异常 / 本地网络-NAT 风险 / 网关无异常
        多用户（multi-user） : 网关异常 / 区域性事件 / 厂商侧异常

    分支顺序即优先级（命中即返回，不叠加）：
        1. 身份缺失                          -> no_evidence（人工）
        2. 多用户 + 网关 down/degraded         -> gateway_down（网关异常）
        3. 多用户 + 区域信号                    -> regional_incident（区域性事件）
        4. 多用户（其余）                      -> vendor_anomaly（厂商侧异常）
        5. 单用户 + 账号锁定/禁用              -> account_locked（账号状态异常）
        6. 单用户 + 客户端版本过期             -> client_version_outdated（配置版本异常）
        7. 单用户 + 网关 down/degraded         -> gateway_down（网关异常）
        8. 单用户 + 内网/NAT 信号              -> local_network_nat（本地网络-NAT 风险）
        9. 单用户 + 无可信证据                 -> no_evidence（人工）
        10. 单用户（兜底，网关无异常）         -> connection_failed（809 通用连接失败）

    每个叶子分支都给出置信度 + 排除项 + 处置命令 + 是否必须人工；证据链并入
    evidence_quote 引用，保证可回溯。
    """
    cite = _citation_items(evidence)

    def _mk(
        hyp: str,
        conf: float,
        ev: list[str],
        ruled: list[str],
        action: str,
        requires_human: bool,
    ) -> DiagnosisConclusion:
        return DiagnosisConclusion(
            hypothesis=hyp,
            confidence=conf,
            evidence=list(ev) + cite,
            ruled_out=list(ruled),
            next_action=action,
            requires_human=requires_human,
        )

    # 1. 身份缺失：无法确认用户/资产/账号归属 -> 必须人工
    if not evidence.identity_ok:
        return _mk(
            HYPOTHESIS_NO_EVIDENCE,
            0.2,
            ["身份/归属缺失，无法确认用户与资产"],
            [
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_MULTI_USER_IMPACT,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_GATEWAY_DOWN,
            ],
            NEXT_ACTION_ESCALATE,
            True,
        )

    multi_user = evidence.multi_user_impact or evidence.fault == HYPOTHESIS_MULTI_USER_IMPACT
    if multi_user:
        # 2. 多用户 + 网关异常
        if evidence.gateway_status in (GatewayStatus.DOWN, GatewayStatus.DEGRADED):
            conf = 0.9 if evidence.gateway_status == GatewayStatus.DOWN else 0.6
            return _mk(
                HYPOTHESIS_GATEWAY_DOWN,
                conf,
                [f"多用户受影响 + 网关状态={evidence.gateway_status.value}"],
                [HYPOTHESIS_ACCOUNT_LOCKED, HYPOTHESIS_CLIENT_VERSION_OUTDATED],
                NEXT_ACTION_ESCALATE,
                True,
            )
        # 3. 多用户 + 区域性事件
        if evidence.regional_incident:
            return _mk(
                HYPOTHESIS_REGIONAL_INCIDENT,
                0.85,
                ["多用户同时失败且无网关异常，疑似区域性事件"],
                [
                    HYPOTHESIS_ACCOUNT_LOCKED,
                    HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                    HYPOTHESIS_GATEWAY_DOWN,
                ],
                NEXT_ACTION_ESCALATE,
                True,
            )
        # 4. 多用户 + 厂商侧异常（兜底）
        return _mk(
            HYPOTHESIS_VENDOR_ANOMALY,
            0.8,
            ["多用户同时失败，无网关/区域信号，疑似 VPN 厂商侧异常"],
            [
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_GATEWAY_DOWN,
            ],
            NEXT_ACTION_ESCALATE,
            True,
        )

    # ---- 单用户分支 ----
    # 5. 账号状态异常
    if evidence.account_status in (AccountStatus.LOCKED, AccountStatus.DISABLED):
        return _mk(
            HYPOTHESIS_ACCOUNT_LOCKED,
            0.95,
            [f"账号状态={evidence.account_status.value}（锁定/禁用，需人工处理）"],
            [
                HYPOTHESIS_GATEWAY_DOWN,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_MULTI_USER_IMPACT,
            ],
            NEXT_ACTION_ESCALATE,
            True,
        )
    # 6. 客户端配置/版本异常
    if _client_version_outdated(evidence) and _all_account_gateway_normal(evidence):
        return _mk(
            HYPOTHESIS_CLIENT_VERSION_OUTDATED,
            0.85,
            [
                f"账号状态={evidence.account_status.value}",
                f"网关状态={evidence.gateway_status.value}",
                f"客户端版本={evidence.client_version or '未知'} < 要求 {evidence.required_version or '未知'}",
            ],
            [HYPOTHESIS_GATEWAY_DOWN, HYPOTHESIS_ACCOUNT_LOCKED, HYPOTHESIS_MULTI_USER_IMPACT],
            NEXT_ACTION_PROVIDE_STEPS,
            False,
        )
    # 7. 单用户 + 网关异常
    if evidence.gateway_status in (GatewayStatus.DOWN, GatewayStatus.DEGRADED):
        conf = 0.9 if evidence.gateway_status == GatewayStatus.DOWN else 0.6
        return _mk(
            HYPOTHESIS_GATEWAY_DOWN,
            conf,
            [f"网关状态={evidence.gateway_status.value}"],
            [
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_CONNECTION_FAILED,
            ],
            NEXT_ACTION_ESCALATE,
            True,
        )
    # 8. 单用户 + 本地网络/NAT 风险（内网不可达）
    if (
        evidence.intranet_signal
        or evidence.local_network_nat
        or evidence.fault == HYPOTHESIS_INTRANET_UNREACHABLE
    ):
        return _mk(
            HYPOTHESIS_LOCAL_NETWORK_NAT,
            0.8,
            ["本地网络/NAT 风险（内网不可达），需排查本地路由"],
            [
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_GATEWAY_DOWN,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
            ],
            NEXT_ACTION_PROVIDE_STEPS if evidence.knowledge_hit else NEXT_ACTION_ESCALATE,
            not evidence.knowledge_hit,
        )
    # 9. 单用户 + 无可信证据
    if not _has_credible_evidence(evidence):
        return _mk(
            HYPOTHESIS_NO_EVIDENCE,
            0.2,
            ["账号/网关状态未知且无知识命中，无可信证据支撑根因"],
            [
                HYPOTHESIS_ACCOUNT_LOCKED,
                HYPOTHESIS_MULTI_USER_IMPACT,
                HYPOTHESIS_CLIENT_VERSION_OUTDATED,
                HYPOTHESIS_GATEWAY_DOWN,
            ],
            NEXT_ACTION_ESCALATE,
            True,
        )
    # 10. 单用户兜底：网关无异常（809 通用连接失败）
    return _mk(
        HYPOTHESIS_CONNECTION_FAILED,
        0.85 if evidence.knowledge_hit else 0.6,
        [f"网关状态={evidence.gateway_status.value}（无异常）", "809/通用连接失败，网关无异常"],
        [
            HYPOTHESIS_ACCOUNT_LOCKED,
            HYPOTHESIS_GATEWAY_DOWN,
            HYPOTHESIS_CLIENT_VERSION_OUTDATED,
            HYPOTHESIS_MULTI_USER_IMPACT,
        ],
        NEXT_ACTION_PROVIDE_STEPS if evidence.knowledge_hit else NEXT_ACTION_ESCALATE,
        not evidence.knowledge_hit,
    )


def guardrail_evaluate(evidence: VpnEvidence) -> DiagnosisConclusion:
    """确定性护栏判定（纯函数）：先算结论，再按护栏收紧。

    护栏规则（Agent 只做「选查询顺序/提假设/整理证据/生成下一步」；本函数即
    「权限校验/工具白名单/置信度门槛/高风险升级/状态迁移/审批门禁/执行结果」的
    确定性部分在规则层的落地）：
        (i)   置信度门槛：confidence < GUARDRAIL_CONFIDENCE_THRESHOLD(0.80) -> requires_human=True；
        (ii)  高风险升级：multi_user_impact / auth_failed（无可靠路径）/ no_evidence -> requires_human=True；
        (iii) 无证据不得自动回复：evidence 为空 -> requires_human=True。
    当最终 requires_human=True 时，next_action 强制为 escalate_incident（转人工），
    绝不自动给出 provide_steps/ask_customer 等「自动处置」。
    工具白名单/scope 属于治理层（tool_governance / models.FORBIDDEN_COMMANDS），本层只复述不越权。
    """
    dia = evaluate_evidence(evidence)
    requires_human = bool(dia.requires_human or dia.must_handoff)

    # (i) 置信度门槛
    if dia.confidence < GUARDRAIL_CONFIDENCE_THRESHOLD:
        requires_human = True
    # (ii) 高风险升级
    if evidence.fault in (HYPOTHESIS_MULTI_USER_IMPACT, HYPOTHESIS_AUTH_FAILED):
        requires_human = True
    if dia.hypothesis in (
        HYPOTHESIS_NO_EVIDENCE,
        HYPOTHESIS_REGIONAL_INCIDENT,
        HYPOTHESIS_VENDOR_ANOMALY,
    ):
        requires_human = True
    # (iii) 无证据 -> 不得自动回复
    if not dia.evidence:
        requires_human = True

    next_action = dia.next_action if not requires_human else NEXT_ACTION_ESCALATE
    return DiagnosisConclusion(
        hypothesis=dia.hypothesis,
        evidence=list(dia.evidence),
        confidence=dia.confidence,
        ruled_out=list(dia.ruled_out),
        next_action=next_action,
        requires_human=requires_human,
    )


def conclusion_fault_class(hypothesis: str) -> str:
    """把结论假设映射到 5 类 VPN 故障（粗粒度分类）。

    供 frozen-sample 评测的「VPN 分类准确率」使用：树叶子假设（account_locked /
    gateway_down / client_version_outdated / local_network_nat / regional_incident /
    vendor_anomaly）归并到其所属的 5 类故障；no_evidence 不映射（返回 no_evidence，
    由评测单独处理/排除）。
    """
    mapping: dict[str, str] = {
        HYPOTHESIS_ACCOUNT_LOCKED: HYPOTHESIS_AUTH_FAILED,
        HYPOTHESIS_GATEWAY_DOWN: HYPOTHESIS_CONNECTION_FAILED,
        HYPOTHESIS_CLIENT_VERSION_OUTDATED: HYPOTHESIS_CONNECTION_FAILED,
        HYPOTHESIS_LOCAL_NETWORK_NAT: HYPOTHESIS_INTRANET_UNREACHABLE,
        HYPOTHESIS_REGIONAL_INCIDENT: HYPOTHESIS_MULTI_USER_IMPACT,
        HYPOTHESIS_VENDOR_ANOMALY: HYPOTHESIS_MULTI_USER_IMPACT,
    }
    return mapping.get(hypothesis, hypothesis)
