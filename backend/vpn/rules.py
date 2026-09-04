"""LANGGraph VPN Diagnosis Agent — 确定性证据链诊断规则（纯函数，无 IO，可单测）。

模块归属：backend/vpn/rules.py（本任务 t3 新增）。定位：
    - 把 VPN 诊断从「关键词分类」升级为「证据链」：不再只给一个 vpn_fault 标签，
      而是对「账号 / 网关 / 客户端版本 / 错误码 / 多用户影响 / 证据充分性」逐条归一化为信号，
      再按 **确定性规则表** 产出结构化诊断结论。
    - 结论固定为结构化：``hypothesis / confidence / evidence[] / ruled_out[] /
      next_action / reason_codes[] / must_handoff``（对齐 docs/evaluation/vpn-eval-metrics.md §3 M3 真实口径）。
    - 与 backend/vpn/models.py 的 ``evaluate_handoff`` 互补：models 只做「是否必须转人工」的
      四类判定；rules 把「根因假设 + 证据链 + 排除项 + 处置命令」一并固化为可评测结构。

规则表（顺序即优先级；命中即返回，不叠加后续）：

    R0 身份缺失(identity_ok=False)              -> no_evidence 人工
    R1 多用户同时失败(multi_user_impact)         -> 事件升级（不走单用户建议）
    R2 账号锁定(account_status=locked)           -> 必须人工(it.account)
    R3 账号正常 + 网关 normal/up + 客户端版本异常 -> 配置/客户端版本问题（root cause=client_version_outdated）
    R4 账号正常 + 网关 down/degraded             -> 网关侧问题（root cause=gateway_down）
    R5 其余账号/网关正常 + 版本正常，仅有错误码信号 -> 连接类失败（error_code 仅作假设信号，不直接等于根因）
    R6 无任何可信证据(account/gateway 未知、无知识命中) -> 不给出根因结论，仅转人工（no_evidence）

设计要点：
    - 纯函数、确定性、无 IO：输入 ``VpnEvidence``（归一化信号快照），输出 ``EvidenceDiagnosis``。
    - 错误码只作为假设信号（reason_codes 标注 error_code_signal=...），**不能直接等于根因**；
      根因由账号/网关/客户端版本的可信状态推出。修复 D6 关联（账号/VPN/网关主问题与关联问题分离）。
    - 单测用 ``tests/test_vpn_evidence_rules.py`` 直接覆盖每条规则与版本比较、信号归一化。
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
HYPOTHESIS_NO_EVIDENCE = "no_evidence"  # 无可信证据：不给出根因结论

# 处置命令（复用 DiagnosisCommandType 允许命令；escalate_incident 为事件升级/转人工）。
NEXT_ACTION_ESCALATE = DiagnosisCommandType.ESCALATE_INCIDENT.value
NEXT_ACTION_PROVIDE_STEPS = DiagnosisCommandType.PROVIDE_STEPS.value
NEXT_ACTION_ASK_CUSTOMER = DiagnosisCommandType.ASK_CUSTOMER.value

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
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list, max_length=32)
    ruled_out: list[str] = Field(default_factory=list, max_length=32)
    next_action: str
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    must_handoff: bool = False

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容 dict（供评测归一化 / 报告消费）。"""
        return {
            "hypothesis": self.hypothesis,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "ruled_out": list(self.ruled_out),
            "next_action": self.next_action,
            "reason_codes": list(self.reason_codes),
            "must_handoff": self.must_handoff,
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
    """
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
        )

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
        )

    # R5 兜底：账号/网关正常，无版本过期，可能有错误码信号 -> 连接类失败
    #     错误码只作为假设信号（reason_codes 标注），不能直接等于根因。
    #     有知识命中时置信度>=0.80（与 must_handoff=False 一致，通过四类门禁）；
    #     无知识命中时置信度下调且强制转人工（不给出根因结论自动建议）。
    reason_codes = ["connection_failed"]
    if evidence.error_code:
        reason_codes.append(f"error_code_signal={evidence.error_code}")
    evidence_notes = [f"账号状态={evidence.account_status.value}", f"网关状态={evidence.gateway_status.value}"]
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
        next_action=(
            NEXT_ACTION_PROVIDE_STEPS if evidence.knowledge_hit else NEXT_ACTION_ESCALATE
        ),
        reason_codes=reason_codes,
        must_handoff=not evidence.knowledge_hit,
    )


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
        HYPOTHESIS_NO_EVIDENCE,
    }:
        return normalized
    # 关键词匹配（顺序即优先级，命中即返回）。
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        (HYPOTHESIS_ACCOUNT_LOCKED, ("账号", "账户", "锁定", "锁住", "被锁")),
        (HYPOTHESIS_MULTI_USER_IMPACT, ("多用户", "多人", "全公司", "整个部门", "集体", "大规模")),
        (HYPOTHESIS_CLIENT_VERSION_OUTDATED, ("版本", "客户端", "升级")),
        (HYPOTHESIS_GATEWAY_DOWN, ("网关", "gateway")),
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
    )


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
_ACCOUNT_LOCKOUT_EXCLUDE_TERMS = ("登录失败", "密码错误", "认证失败", "身份验证", "验证码", "用户名", "证书")


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
