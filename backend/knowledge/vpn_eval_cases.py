"""VPN 专项评测集（60 条，脱敏，vpn-v1）。

版本：VPN_EVAL_VERSION（冻结；变更须递增版本并留痕）。
场景分布（共 60 条）：connection_failed 12 / frequent_disconnect 10 /
auth_failed 10 / intranet_unreachable 10 / multi_user_impact 8 /
负向+越界 10。

每条样本固定记录：场景、vpn_fault、8 项固定字段、预期分类（单一主线 it.vpn）、
8 项必填字段、目标团队、预期知识文档、边界三态（auto_suggest / must_ask /
must_escalate）；负向/越界样本 expected_category 可能不是 it.vpn，且要求
不被误导向自动处置（boundary 必须 must_escalate）。

设计：
    - 分类预期与内置 KeywordTicketClassifier + classify_vpn_fault 对齐（回归可重复）；
    - knowledge 检索不在本集内执行；expected_document_ids 代表“应有依据”，
      实际是否命中由 backend/run_vpn_eval.py 结合门禁验证；
    - provided_fields 必须与 expected_boundary 一致：must_ask 必缺字段、
      auto_suggest 必齐 8 项、must_escalate 场景各自合理；
    - 全部为脱敏构造文本，不包含真实客户数据。
"""

from __future__ import annotations

from typing import NotRequired, TypedDict

from src.my_agent.helpdesk.intake import (
    BOUNDARY_AUTO_SUGGEST,
    BOUNDARY_MUST_ASK,
    BOUNDARY_MUST_ESCALATE,
    VPN_FAULT_AUTH_FAILED,
    VPN_FAULT_CONNECTION_FAILED,
    VPN_FAULT_FREQUENT_DISCONNECT,
    VPN_FAULT_INTRANET_UNREACHABLE,
    VPN_FAULT_MULTI_USER_IMPACT,
)


class VpnEvalCase(TypedDict):
    """一条 VPN 专项评测样本（vpn-v1 口径）。"""

    scenario: str
    text: str
    vpn_fault: str
    provided_fields: dict[str, str]
    expected_category: str
    required_fields: tuple[str, ...]
    expected_team: str
    expected_document_ids: tuple[str, ...]
    expected_boundary: str
    is_negative: NotRequired[bool]


VPN_EVAL_VERSION = "2026-09-05-vpn-v1"

# 8 项固定字段（scope 文档第 3 节；与 IntakePolicy 的 it.vpn 租户策略必填项一致）
VPN_REQUIRED_FIELDS: tuple[str, ...] = (
    "device",
    "operating_system",
    "vpn_client",
    "client_version",
    "error_code",
    "network",
    "multi_user_impacted",
    "recent_change",
)

# 预期知识文档（与 backend.seed_demo 的 VPN 脱敏文档对应）
DOC_BY_CATEGORY: dict[str, tuple[str, ...]] = {"vpn": ("vpn-001",)}


def _full_vpn_fields(text: str) -> dict[str, str]:
    """8 项 VPN 固定字段全部就绪（用于 auto_suggest / must_escalate 用例）。"""
    return {
        "device": "laptop-001",
        "operating_system": "Windows 11",
        "vpn_client": "OpenVPN",
        "client_version": "3.4.2",
        "error_code": "无",
        "network": "家庭宽带",
        "multi_user_impacted": "否",
        "recent_change": "无",
    }


def _vpn_missing(text: str, missing: tuple[str, ...]) -> dict[str, str]:
    """字段缺失用例：从完整 8 项字段里去掉指定必填字段。"""
    fields = _full_vpn_fields(text)
    for name in missing:
        fields.pop(name, None)
    return fields


def _case(
    *,
    scenario: str,
    text: str,
    vpn_fault: str,
    provided_fields: dict[str, str],
    boundary: str,
    expected_category: str = "it.vpn",
    expected_team: str = "team-it",
    documents: tuple[str, ...] = ("vpn-001",),
    is_negative: bool = False,
) -> VpnEvalCase:
    """构造一条 VpnEvalCase（默认 8 项字段 + it.vpn + vpn-001 依据）。"""
    return {
        "scenario": scenario,
        "text": text,
        "vpn_fault": vpn_fault,
        "provided_fields": provided_fields,
        "expected_category": expected_category,
        "required_fields": VPN_REQUIRED_FIELDS,
        "expected_team": expected_team,
        "expected_document_ids": documents,
        "expected_boundary": boundary,
        "is_negative": is_negative,
    }


def _auto(text: str, fault: str) -> VpnEvalCase:
    """字段齐 + 无风险 + 有依据 -> auto_suggest。"""
    return _case(
        scenario=fault,
        text=text,
        vpn_fault=fault,
        provided_fields=_full_vpn_fields(text),
        boundary=BOUNDARY_AUTO_SUGGEST,
    )


def _ask(text: str, fault: str, missing: tuple[str, ...]) -> VpnEvalCase:
    """VPN 分类正确但必填字段不全 -> must_ask。"""
    return _case(
        scenario=fault,
        text=text,
        vpn_fault=fault,
        provided_fields=_vpn_missing(text, missing),
        boundary=BOUNDARY_MUST_ASK,
    )


def _escalate(text: str, fault: str) -> VpnEvalCase:
    """字段齐但认证/群体故障 -> must_escalate。"""
    return _case(
        scenario=fault,
        text=text,
        vpn_fault=fault,
        provided_fields=_full_vpn_fields(text),
        boundary=BOUNDARY_MUST_ESCALATE,
    )


_CF = VPN_FAULT_CONNECTION_FAILED
_FD = VPN_FAULT_FREQUENT_DISCONNECT
_AF = VPN_FAULT_AUTH_FAILED
_IU = VPN_FAULT_INTRANET_UNREACHABLE
_MU = VPN_FAULT_MULTI_USER_IMPACT


def _connection_failed_cases() -> list[VpnEvalCase]:
    return [
        _auto("公司 VPN 连不上，提示错误码 809", _CF),
        _auto("远程办公 VPN 一直转圈，无法建立连接", _CF),
        _auto("VPN 无法联网，一直显示连接中", _CF),
        _auto("外网访问内网 VPN 连接超时", _CF),
        _auto("公司 VPN 接入失败，提示错误码 800", _CF),
        _auto("VPN 客户端连接不了，一直转圈", _CF),
        _auto("出差连公司 VPN，提示无法建立连接", _CF),
        _auto("OpenVPN 一直连接不上，错误码 809", _CF),
        _auto("VPN 无法建立连接，虚拟网卡驱动异常", _CF),
        _auto("公司 VPN 连接失败，无法访问外网", _CF),
        _ask("VPN 连不上", _CF, ("device", "network")),
        _ask("外网 VPN 无法连接", _CF, ("vpn_client", "error_code")),
    ]


def _frequent_disconnect_cases() -> list[VpnEvalCase]:
    return [
        _auto("VPN 登录后频繁掉线", _FD),
        _auto("VPN 不断重连，网络不稳定", _FD),
        _auto("远程 VPN 经常掉线，一用就断开", _FD),
        _auto("VPN 老是自动断开，反复连接", _FD),
        _auto("公司 VPN 频繁断线，无法继续工作", _FD),
        _auto("VPN 连接不稳定，频繁掉线", _FD),
        _auto("远程接入 VPN 频繁断开重连", _FD),
        _auto("VPN 登录成功但几分钟就掉线", _FD),
        _ask("VPN 频繁掉线", _FD, ("operating_system", "client_version")),
        _ask("VPN 一直掉线", _FD, ("multi_user_impacted", "recent_change")),
    ]


def _auth_failed_cases() -> list[VpnEvalCase]:
    return [
        _escalate("VPN 认证失败，用户名或密码错误", _AF),
        _escalate("VPN 登录提示证书过期", _AF),
        _escalate("VPN 用户名密码错误，错误码 691", _AF),
        _escalate("公司 VPN 身份验证失败", _AF),
        _escalate("VPN 登录失败，账号被锁定", _AF),
        _escalate("VPN 证书已过期，无法认证", _AF),
        _escalate("VPN 认证失败，登录验证码错误", _AF),
        _escalate("远程 VPN 提示错误码 691，认证失败", _AF),
        _ask("VPN 认证失败", _AF, ("device", "vpn_client")),
        _ask("VPN 密码错误", _AF, ("operating_system", "network")),
    ]


def _intranet_unreachable_cases() -> list[VpnEvalCase]:
    return [
        _auto("VPN 能连上但访问不了内网", _IU),
        _auto("VPN 远程桌面不通，ping 不通内网", _IU),
        _auto("连上 VPN 后内网不可达", _IU),
        _auto("VPN 无法访问内网服务器", _IU),
        _auto("VPN 无法访问内网资源，远程桌面不通", _IU),
        _auto("远程接入 VPN 后访问不了内网", _IU),
        _auto("VPN 访问不了内网，ping 不通", _IU),
        _auto("连上 VPN 无法访问内网，远程桌面不通", _IU),
        _ask("VPN 内网不可达", _IU, ("vpn_client", "client_version")),
        _ask("VPN 访问不了内网", _IU, ("error_code", "network")),
    ]


def _multi_user_impact_cases() -> list[VpnEvalCase]:
    return [
        _escalate("全公司都无法连接 VPN", _MU),
        _escalate("整个部门 VPN 都连不上", _MU),
        _escalate("多人反映 VPN 频繁掉线", _MU),
        _escalate("所有同事 VPN 都无法访问内网", _MU),
        _escalate("大面积 VPN 连接失败", _MU),
        _escalate("团队多人 VPN 认证失败", _MU),
        _escalate("VPN 大规模故障，大家都连不上", _MU),
        _escalate("多个同事同时 VPN 掉线", _MU),
    ]


def _negative_cases() -> list[VpnEvalCase]:
    """负向/越界样例（越界大类 + 高风险 VPN 文本），一律不得自动建议。"""
    return [
        _case(
            scenario="negative",
            text="办公区断网了",
            vpn_fault=_CF,
            provided_fields=_full_vpn_fields("办公区断网了"),
            expected_category="it.network",
            documents=(),
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="SSO 登录密码错误",
            vpn_fault=_AF,
            provided_fields=_full_vpn_fields("SSO 登录密码错误"),
            expected_category="it.account",
            documents=(),
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="报销发票付款流程咨询",
            vpn_fault=_CF,
            provided_fields=_full_vpn_fields("报销发票付款流程咨询"),
            expected_category="finance",
            documents=(),
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="VPN 全公司都无法连接",
            vpn_fault=_MU,
            provided_fields=_full_vpn_fields("VPN 全公司都无法连接"),
            expected_category="it.vpn",
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="VPN 删除数据",
            vpn_fault=_CF,
            provided_fields=_full_vpn_fields("VPN 删除数据"),
            expected_category="it.vpn",
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="VPN 生产环境数据泄露",
            vpn_fault=_CF,
            provided_fields=_full_vpn_fields("VPN 生产环境数据泄露"),
            expected_category="it.vpn",
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="如何申请企业微信审批",
            vpn_fault=_CF,
            provided_fields=_full_vpn_fields("如何申请企业微信审批"),
            expected_category="other",
            documents=(),
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="VPN 连不上，怎么排查",
            vpn_fault=_CF,
            provided_fields=_full_vpn_fields("VPN 连不上，怎么排查"),
            expected_category="it.vpn",
            documents=(),
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="打印机走网络打印不了",
            vpn_fault=_CF,
            provided_fields=_full_vpn_fields("打印机走网络打印不了"),
            expected_category="it.printer",
            documents=(),
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
        _case(
            scenario="negative",
            text="邮件收不到，Outlook 报错",
            vpn_fault=_CF,
            provided_fields=_full_vpn_fields("邮件收不到，Outlook 报错"),
            expected_category="it.email",
            documents=(),
            boundary=BOUNDARY_MUST_ESCALATE,
            is_negative=True,
        ),
    ]


def _build_cases() -> list[VpnEvalCase]:
    return [
        *_connection_failed_cases(),
        *_frequent_disconnect_cases(),
        *_auth_failed_cases(),
        *_intranet_unreachable_cases(),
        *_multi_user_impact_cases(),
        *_negative_cases(),
    ]


VPN_EVAL_CASES: tuple[VpnEvalCase, ...] = tuple(_build_cases())


def count() -> int:
    """VPN 专项评测集总条数。"""
    return len(VPN_EVAL_CASES)


def fault_counts() -> dict[str, int]:
    """vpn_fault 条数分布（负向/越界样例并入 independent 桶）。

    参考口径：connection_failed 12 / frequent_disconnect 10 / auth_failed 10 /
    intranet_unreachable 10 / multi_user_impact 8 / 负向+越界 10 = 60。
    """
    counts: dict[str, int] = {}
    for case in VPN_EVAL_CASES:
        if case.get("is_negative"):
            counts["negative_out_of_scope"] = counts.get("negative_out_of_scope", 0) + 1
            continue
        fault = case["vpn_fault"]
        counts[fault] = counts.get(fault, 0) + 1
    return counts


def boundary_counts() -> dict[str, int]:
    """按 expected_boundary 三态统计条数分布。"""
    counts: dict[str, int] = {}
    for case in VPN_EVAL_CASES:
        boundary = case["expected_boundary"]
        counts[boundary] = counts.get(boundary, 0) + 1
    return counts
