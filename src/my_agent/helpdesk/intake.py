"""确定性受理规则：分类契约、内置分类器、必填字段与派单决策。

职责：
    - TicketCategory / RiskLevel：分类与风险枚举
    - IntakePolicy：按分类配置必填字段、追问上限、默认目标团队
    - KeywordTicketClassifier：可解释的关键词分类器（基线实现，可替换为校准模型）
    - assess_and_dispatch：敏感/高影响词识别 + 派单决策（团队/优先级/风险/原因码）

关键设计：
    - 分类与派单都是纯函数、确定性逻辑：不依赖模型时行为可预期、可单测
    - 置信度规则显式写在 docstring 里，低置信度/多类并列转人工复核
    - 敏感词（删除数据/开通权限/转账...）与高影响词（全公司/生产环境...）
      直接映射为 risk=high / priority=urgent，是安全前置规则而非模型判断
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class TicketCategory(StrEnum):
    """工单大类；与 tenant_it_policies 的 category 前缀对应。"""

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
    """风险等级：驱动优先级与是否触发额外审批/人工。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClassificationResult(BaseModel):
    """分类结果：大类/子分类 + 命中的信号词 + 置信度 + 是否需人工复核。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: TicketCategory
    subcategory: str = Field(default="general", min_length=1, max_length=64)
    signals: tuple[str, ...] = ()
    needs_human_review: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DispatchDecision(BaseModel):
    """派单决策：目标团队 + 优先级 + 风险等级 + 原因码（审计用）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str = Field(min_length=1, max_length=128)
    priority: str = Field(pattern=r"^(low|normal|high|urgent)$")
    risk_level: RiskLevel
    reason_codes: tuple[str, ...]


class TicketClassifier(Protocol):
    """分类器协议：任何实现（关键词/LLM）都返回 ClassificationResult。"""

    async def classify(self, text: str, fields: Mapping[str, Any]) -> ClassificationResult: ...


@dataclass(frozen=True, slots=True)
class IntakePolicy:
    """内置受理策略：必填字段、追问次数上限、分类 -> 团队映射。

    租户级策略（backend/tickets/policies.py）会在此基础上叠加覆盖。

    V1 范围约束：supported_categories 是允许「自动受理/自动派单」的大类白名单。
    默认只允许 IT 类进入自动受理；finance / admin / product / other 命中后
    统一转服务台人工队列（team_service_desk），不再自动派至对应业务团队。
    """

    base_required_fields: frozenset[str] = frozenset({"title", "description", "requester_id"})
    # V1 默认受理范围：仅 IT；越界大类一律人工队列（可被测试/租户覆盖）
    supported_categories: frozenset[TicketCategory] = field(
        default_factory=lambda: frozenset({TicketCategory.IT})
    )
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
        # 大类关键词：命中即产生分类信号
        TicketCategory.IT: (
            "登录",
            "密码",
            "网络",
            "断网",
            "电脑",
            "系统",
            "vpn",
            "sso",
            "故障",
            "账号",
            "邮箱",
            "outlook",
            "邮件",
            "显示器",
            "软件",
            "打印机",
            "手机",
            "权限",
            # 网络/账号高频词（Day 3 固定评测集覆盖，提升网络场景识别）
            "wifi",
            "wi-fi",
            "网速",
            "ip",
            "dns",
            "网关",
            "交换机",
            "网线",
            "信号",
            "丢包",
            "延迟",
            "路由",
            "代理",
            "内网",
            "外网",
            "视频会议",
            "mfa",
            "验证码",
        ),
        TicketCategory.FINANCE: ("报销", "发票", "付款", "工资", "财务", "费用"),
        TicketCategory.ADMIN: ("门禁", "工位", "会议室", "采购", "行政", "用印"),
        TicketCategory.PRODUCT: ("产品", "功能", "页面", "bug", "版本", "订单"),
    }

    _IT_SUBCATEGORY_KEYWORDS: Mapping[str, tuple[str, ...]] = {
        # IT 子分类关键词：命中则细分到 it.vpn / it.account 等
        # 顺序优先级：printer 在 network 之前，避免「打印机 + 网络」被网络误改
        "vpn": ("vpn", "远程接入", "无法联网", "外网"),
        "account": ("账号", "登录", "密码", "sso", "锁定", "重置密码", "mfa", "验证码"),
        "email": ("邮箱", "邮件", "outlook", "收不到邮件", "退信"),
        "hardware": ("电脑", "显示器", "键盘", "鼠标", "主板", "硬件", "电源"),
        "software": ("软件", "安装", "更新", "蓝屏", "系统崩溃", "应用"),
        "printer": ("打印机", "打印", "复印", "硒鼓"),
        "network": (
            "网络",
            "断网",
            "wifi",
            "wi-fi",
            "局域网",
            "网速",
            "网关",
            "ip",
            "dns",
            "交换机",
            "网线",
            "信号",
            "丢包",
            "延迟",
            "路由",
            "代理",
            "内网",
            "外网",
            "视频会议",
        ),
        "permission": ("权限", "授权", "申请权限", "访问控制", "角色", "开通", "acl"),
    }

    async def classify(self, text: str, fields: Mapping[str, Any]) -> ClassificationResult:
        """按关键词命中数分类：最多信号的大类胜出，IT 再细分到子分类。"""
        normalized = text.casefold()
        matches: list[tuple[TicketCategory, list[str]]] = []
        for category, keywords in self._KEYWORDS.items():
            signals = [keyword for keyword in keywords if keyword.casefold() in normalized]
            if signals:
                matches.append((category, signals))
        matches.sort(key=lambda item: len(item[1]), reverse=True)
        if not matches:
            # 无任何信号：归 OTHER 且需人工复核（置信度 0.2）
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
            # D6：账号锁定为主因时优先归 it.account（即使文本同时含 "vpn/远程接入"）。
            if _is_account_lockout_primary(normalized):
                subcategory = "account"
                confidence = min(1.0, confidence + 0.1)
            else:
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


# 敏感操作词 -> risk=high；高影响词 -> risk=high + priority=urgent
_SENSITIVE_TERMS = ("删除数据", "开通权限", "管理员权限", "转账", "付款审批", "离职")
_HIGH_IMPACT_TERMS = ("全部用户", "全公司", "生产环境", "业务中断", "无法办公", "数据泄露")

# D6 修复：账号锁定-VPN 混淆。历史缺口（summary-vpn-v2.md D6）——
# 含字面 "VPN/远程接入" 的文本被 it.vpn 子分类优先命中（vpn 先于 account），导致 S6
# 5 条本应归 it.account（账号锁定为主因）的样本被归 it.vpn。这里做「主问题/关联问题分离」：
# 当文本显式表达账号锁定为主因（_ACCOUNT_LOCKOUT_PRIMARY_TERMS）且**不落入** VPN 认证失败
# 框架（v1 的 "VPN 登录失败，账号被锁定" 仍是 it.vpn 认证类），优先归 it.account。
_ACCOUNT_LOCKOUT_PRIMARY_TERMS = (
    "账号被锁定",
    "账号被锁",
    "账户被锁",
    "账户锁定",
    "账号锁定",
    "锁定导致",
    "被锁定",
    "被锁了",
    "被锁死",
    "锁住了",
)
# VPN 认证/登录失败框架：这类文本的账号锁定是认证失败的伴生结果，主因仍是 VPN 认证类（it.vpn）。
_VPN_AUTH_FAILURE_TERMS = ("登录失败", "密码错误", "认证失败", "身份验证", "验证码", "用户名", "证书")


def _is_account_lockout_primary(text: str) -> bool:
    """判定账号锁定是否为**主问题**（而非 VPN 认证失败的伴生结果）。

    D6：命中账号锁定主因词列表，且未落入 VPN 认证失败框架时返回 True，
    供 it 子分类优先归 it.account（与 vpn 关键词共存时账号主因优先）。
    """
    normalized = text.casefold()
    lockout_hit = any(term in normalized for term in _ACCOUNT_LOCKOUT_PRIMARY_TERMS)
    if not lockout_hit:
        return False
    # 若同时落入 VPN 认证/登录失败框架，则视为 VPN 认证类为主（保留 it.vpn）。
    if any(term in normalized for term in _VPN_AUTH_FAILURE_TERMS):
        return False
    return True

# ---- VPN V1 专项（docs/product/vpn-v1-scope.md 第 2/3/4 节）----
# 固定 5 类 VPN 故障（vpn_fault 枚举值），作为 it.vpn 的纵向故障维度。
VPN_FAULT_CONNECTION_FAILED = "connection_failed"
VPN_FAULT_FREQUENT_DISCONNECT = "frequent_disconnect"
VPN_FAULT_AUTH_FAILED = "auth_failed"
VPN_FAULT_INTRANET_UNREACHABLE = "intranet_unreachable"
VPN_FAULT_MULTI_USER_IMPACT = "multi_user_impact"
VPN_FAULTS: tuple[str, ...] = (
    VPN_FAULT_CONNECTION_FAILED,
    VPN_FAULT_FREQUENT_DISCONNECT,
    VPN_FAULT_AUTH_FAILED,
    VPN_FAULT_INTRANET_UNREACHABLE,
    VPN_FAULT_MULTI_USER_IMPACT,
)

# 场景边界矩阵的三态（scope 文档第 4 节）
BOUNDARY_AUTO_SUGGEST = "auto_suggest"
BOUNDARY_MUST_ASK = "must_ask"
BOUNDARY_MUST_ESCALATE = "must_escalate"

# vpn_fault 关键词判定（顺序即优先级：群体 > 认证 > 频繁掉线 > 内网 > 连接）。
# 每张 VPN 工单必须且只归一类的兜底：无任何信号时归 connection_failed。
_VPN_FAULT_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    VPN_FAULT_MULTI_USER_IMPACT: (
        "多人", "整个部门", "整个团队", "全公司", "所有人", "所有同事",
        "同事", "大面积", "集体", "大规模", "大家", "同时",
    ),
    VPN_FAULT_AUTH_FAILED: (
        "认证失败", "身份验证", "用户名或密码", "用户名密码", "密码错误",
        "账号密码", "证书过期", "证书无效", "证书已过期", "错误码691",
        "错误码 691", "验证码", "登录失败",
    ),
    VPN_FAULT_FREQUENT_DISCONNECT: (
        "频繁掉线", "频繁断开", "频繁断线", "不断重连", "反复重连",
        "经常掉线", "老是掉线", "反复掉线", "一直掉线", "自动断开",
        "连接不稳定", "网络不稳定", "掉线", "断线",
    ),
    VPN_FAULT_INTRANET_UNREACHABLE: (
        "内网不可达", "访问不了内网", "内网不通", "无法访问内网",
        "ping不通", "ping 不通", "远程桌面不通", "打不开内网",
        "访问不了服务器", "内网访问不了", "无法访问内网资源",
    ),
    VPN_FAULT_CONNECTION_FAILED: (
        "连不上", "无法建立连接", "无法连接", "连接失败", "一直转圈",
        "连接超时", "接入失败", "无法联网", "建立连接", "错误码809",
        "错误码 809", "错误码800", "错误码 800",
    ),
}


def classify_vpn_fault(text: str) -> str:
    """按关键词判定 vpn_fault（确定性基线，默认 connection_failed）。

    优先级：multi_user_impact > auth_failed > frequent_disconnect >
    intranet_unreachable > connection_failed；无任何信号时归 connection_failed，
    作为「只能归一类且必须归一类的兜底」。只做关键词判定，不改变既有 classify()。
    """
    normalized = text.casefold()
    for fault, keywords in _VPN_FAULT_KEYWORDS.items():
        if any(keyword.casefold() in normalized for keyword in keywords):
            return fault
    return VPN_FAULT_CONNECTION_FAILED


def contains_sensitive_term(text: str) -> bool:
    """是否命中敏感词（删除数据/开通权限等）——风险判定前置规则。"""
    normalized = text.casefold()
    return any(term.casefold() in normalized for term in _SENSITIVE_TERMS)


def contains_high_impact_term(text: str) -> bool:
    """是否命中高影响词（全公司/生产环境等）——群体/关键业务影响判定。"""
    normalized = text.casefold()
    return any(term.casefold() in normalized for term in _HIGH_IMPACT_TERMS)


def boundary_vpn(
    vpn_fault: str,
    fields_complete: bool,
    has_sensitive_risk: bool,
    has_high_impact: bool,
    has_evidence: bool,
    *,
    has_account_lockout: bool = False,
) -> str:
    """VPN 场景边界矩阵（scope 文档第 4 节决策函数，确定性、可单测）。

    返回三态之一：auto_suggest / must_ask / must_escalate。
    顺序严格：字段不全先追问 -> 账号锁定/禁用升级（D6：账号锁定必须 it.account + 人工）
    -> 认证类高风险升级 -> 群体故障升级 -> 敏感/高影响升级 -> 无依据升级 -> 允许自动建议。
    """
    if not fields_complete:
        return BOUNDARY_MUST_ASK
    # D6：账号锁定/禁用 -> 必须人工（it.account），不给任何 VPN 自动建议。
    if has_account_lockout:
        return BOUNDARY_MUST_ESCALATE
    if vpn_fault == VPN_FAULT_AUTH_FAILED:
        return BOUNDARY_MUST_ESCALATE
    if vpn_fault == VPN_FAULT_MULTI_USER_IMPACT:
        return BOUNDARY_MUST_ESCALATE
    if has_sensitive_risk or has_high_impact:
        return BOUNDARY_MUST_ESCALATE
    if not has_evidence:
        return BOUNDARY_MUST_ESCALATE
    return BOUNDARY_AUTO_SUGGEST


def normalize_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """清洗字段：去空 key、去除字符串首尾空白，返回普通 dict。"""
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
    """返回缺失的必填字段（按名称排序），供追问与完整性检查使用。"""
    missing = [
        name for name in policy.required_fields(category) if fields.get(name) in (None, "", [], {})
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
    """派单决策：敏感/高影响词提风险，分类复核/追问耗尽/未知分类进原因码。

    优先级规则：命中高影响词 -> urgent；其余 normal；
    风险：命中敏感词或高影响词 -> high，否则 low。原因码完整记录决策依据。
    V1 范围规则：不在 supported_categories 内的大类（finance/admin/product/
    other）一律转服务台人工队列并追加 out_of_scope_manual_review 原因码。
    """
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
    in_scope = category in policy.supported_categories
    if not in_scope:
        reasons.append("out_of_scope_manual_review")
    if not reasons:
        reasons.append("category_rule")

    target_team = (
        policy.team_by_category[category]
        if in_scope
        else policy.team_by_category[TicketCategory.OTHER]
    )
    return DispatchDecision(
        team_id=target_team,
        priority=priority,
        risk_level=risk,
        reason_codes=tuple(reasons),
    )


def clarification_question(missing_fields: Sequence[str]) -> str:
    """生成追问文本：列出全部缺失字段。"""
    return "请补充以下信息：" + "、".join(missing_fields)
