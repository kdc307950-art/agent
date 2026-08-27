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
    """Explainable baseline classifier; replaceable by a calibrated classifier."""

    _KEYWORDS: Mapping[TicketCategory, tuple[str, ...]] = {
        TicketCategory.IT: ("登录", "密码", "网络", "电脑", "系统", "vpn", "sso", "故障"),
        TicketCategory.FINANCE: ("报销", "发票", "付款", "工资", "财务", "费用"),
        TicketCategory.ADMIN: ("门禁", "工位", "会议室", "采购", "行政", "用印"),
        TicketCategory.PRODUCT: ("产品", "功能", "页面", "bug", "版本", "订单"),
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
            )
        category, signals = matches[0]
        tied = len(matches) > 1 and len(matches[1][1]) == len(signals)
        return ClassificationResult(
            category=category,
            signals=tuple(signals),
            needs_human_review=tied,
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
