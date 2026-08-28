"""Deterministic intake, classification contracts, and dispatch rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field


class TicketCategory(StrEnum):
    IT = "it"
    FINANCE = "finance"
    ADMIN = "admin"
    PRODUCT = "product"
    OTHER = "other"


# IT 大类下的 8 个子分类（首个版本产品范围），用于 it_policies 的 category 键
# （it.vpn / it.account / it.permission ...）。
IT_SUBCATEGORIES: tuple[str, ...] = (
    "vpn",
    "account",
    "network",
    "email",
    "hardware",
    "software",
    "printer",
    "permission",
)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClassificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: TicketCategory
    subcategory: str = Field(default="general", min_length=1, max_length=64)
    signals: tuple[str, ...] = ()
    needs_human_review: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DispatchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str = Field(min_length=1, max_length=128)
    priority: str = Field(pattern=r"^(low|normal|high|urgent)$")
    risk_level: RiskLevel
    reason_codes: tuple[str, ...]


class TicketClassifier(Protocol):
    async def classify(self, text: str, fields: Mapping[str, Any]) -> ClassificationResult: ...


@dataclass(frozen=True, slots=True)
class IntakePolicy:
    base_required_fields: frozenset[str] = frozenset({"title", "description", "requester_id"})
    category_required_fields: Mapping[TicketCategory, frozenset[str]] = field(
        default_factory=lambda: {
            TicketCategory.IT: frozenset({"affected_system", "impact"}),
            TicketCategory.FINANCE: frozenset({"finance_topic"}),
            TicketCategory.ADMIN: frozenset({"request_type"}),
            TicketCategory.PRODUCT: frozenset({"product_name", "impact"}),
            TicketCategory.OTHER: frozenset(),
        }
    )
    max_clarification_rounds: Mapping[TicketCategory, int] = field(
        default_factory=lambda: {
            TicketCategory.IT: 3,
            TicketCategory.FINANCE: 2,
            TicketCategory.ADMIN: 2,
            TicketCategory.PRODUCT: 3,
            TicketCategory.OTHER: 1,
        }
    )
    team_by_category: Mapping[TicketCategory, str] = field(
        default_factory=lambda: {
            TicketCategory.IT: "team-it",
            TicketCategory.FINANCE: "team-finance",
            TicketCategory.ADMIN: "team-admin",
            TicketCategory.PRODUCT: "team-product",
            TicketCategory.OTHER: "team-service-desk",
        }
    )

    def required_fields(self, category: TicketCategory) -> frozenset[str]:
        return self.base_required_fields | self.category_required_fields.get(category, frozenset())

    def clarification_limit(self, category: TicketCategory) -> int:
        return int(self.max_clarification_rounds.get(category, 1))


class KeywordTicketClassifier:
    """Explainable baseline classifier; replaceable by a calibrated classifier.

    置信度规则（可解释、不依赖模型）：
      - 大类信号 >= 2 个 -> 0.9；1 个 -> 0.6；
      - IT 子分类关键字命中额外 +0.1（上限 1.0）；
      - 多类并列 -> 0.5；无任何信号 -> 0.2；
      - confidence < 0.5 或并列或未识别 -> needs_human_review=True（低置信度转人工）。
    """

    _KEYWORDS: Mapping[TicketCategory, tuple[str, ...]] = {
        TicketCategory.IT: (
            "登录", "密码", "网络", "断网", "电脑", "系统", "vpn", "sso", "故障",
            "账号", "邮箱", "outlook", "邮件", "显示器", "软件", "打印机", "手机", "权限",
        ),
        TicketCategory.FINANCE: ("报销", "发票", "付款", "工资", "财务", "费用"),
        TicketCategory.ADMIN: ("门禁", "工位", "会议室", "采购", "行政", "用印"),
        TicketCategory.PRODUCT: ("产品", "功能", "页面", "bug", "版本", "订单"),
    }

    _IT_SUBCATEGORY_KEYWORDS: Mapping[str, tuple[str, ...]] = {
        "vpn": ("vpn", "远程接入", "无法联网", "外网"),
        "account": ("账号", "登录", "密码", "sso", "锁定", "重置密码"),
        "network": ("网络", "断网", "wifi", "局域网", "网速", "网关"),
        "email": ("邮箱", "邮件", "outlook", "收不到邮件", "退信"),
        "hardware": ("电脑", "显示器", "键盘", "鼠标", "主板", "硬件", "电源"),
        "software": ("软件", "安装", "更新", "蓝屏", "系统崩溃", "应用"),
        "printer": ("打印机", "打印", "复印", "硒鼓"),
        "permission": ("权限", "授权", "申请权限", "访问控制", "角色", "开通", "acl"),
    }

    async def classify(self, text: str, fields: Mapping[str, Any]) -> ClassificationResult:
        normalized = text.casefold()
        matches: list[tuple[TicketCategory, list[str]]] = []
        for category, keywords in self._KEYWORDS.items():
            signals = [keyword for keyword in keywords if keyword.casefold() in normalized]
            if signals:
                matches.append((category, signals))
        matches.sort(key=lambda item: len(item[1]), reverse=True)
        if not matches:
            return ClassificationResult(
                category=TicketCategory.OTHER,
                signals=(),
                needs_human_review=True,
                confidence=0.2,
            )
        category, signals = matches[0]
        tied = len(matches) > 1 and len(matches[1][1]) == len(signals)
        if len(signals) >= 2:
            confidence = 0.9
        else:
            confidence = 0.6
        subcategory = "general"
        if category == TicketCategory.IT:
            for key, keywords in self._IT_SUBCATEGORY_KEYWORDS.items():
                if any(keyword.casefold() in normalized for keyword in keywords):
                    subcategory = key
                    confidence = min(1.0, confidence + 0.1)
                    break
        if tied:
            confidence = min(confidence, 0.5)
        needs_review = tied or confidence < 0.5
        return ClassificationResult(
            category=category,
            subcategory=subcategory,
            signals=tuple(signals),
            needs_human_review=needs_review,
            confidence=confidence,
        )


_SENSITIVE_TERMS = ("删除数据", "开通权限", "管理员权限", "转账", "付款审批", "离职")
_HIGH_IMPACT_TERMS = ("全部用户", "全公司", "生产环境", "业务中断", "无法办公", "数据泄露")


def normalize_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in fields.items():
        safe_key = str(key).strip()
        if not safe_key:
            continue
        if isinstance(value, str):
            value = value.strip()
        normalized[safe_key] = value
    return normalized


def missing_required_fields(
    fields: Mapping[str, Any],
    category: TicketCategory,
    policy: IntakePolicy,
) -> tuple[str, ...]:
    missing = [
        name
        for name in policy.required_fields(category)
        if fields.get(name) in (None, "", [], {})
    ]
    return tuple(sorted(missing))


def assess_and_dispatch(
    *,
    text: str,
    category: TicketCategory,
    classification_needs_review: bool,
    clarification_exhausted: bool,
    policy: IntakePolicy,
) -> DispatchDecision:
    normalized = text.casefold()
    reasons: list[str] = []
    risk = RiskLevel.LOW
    priority = "normal"

    if any(term.casefold() in normalized for term in _SENSITIVE_TERMS):
        risk = RiskLevel.HIGH
        reasons.append("sensitive_operation")
    if any(term.casefold() in normalized for term in _HIGH_IMPACT_TERMS):
        risk = RiskLevel.HIGH
        priority = "urgent"
        reasons.append("high_impact")
    if classification_needs_review:
        reasons.append("classification_review")
    if clarification_exhausted:
        reasons.append("clarification_exhausted")
    if category == TicketCategory.OTHER:
        reasons.append("unknown_category")
    if not reasons:
        reasons.append("category_rule")

    return DispatchDecision(
        team_id=policy.team_by_category[category],
        priority=priority,
        risk_level=risk,
        reason_codes=tuple(reasons),
    )


def clarification_question(missing_fields: Sequence[str]) -> str:
    return "请补充以下信息：" + "、".join(missing_fields)
