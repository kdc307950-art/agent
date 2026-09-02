"""IT 服务台 V1 固定工单评测集（脱敏，Day 3）。

版本：TICKET_EVAL_VERSION（冻结；变更须递增版本并留痕）。
场景分布（共 90 条）：vpn 30 / account 20 / network 20 /
fields_missing 10 / no_knowledge 5 / acl 5。

每条样本固定记录：输入文本、关联资产、预期分类、必填字段、目标团队、
预期知识文档、是否应人工接管；acl 用例还记录禁止命中的文档。

设计：
    - 分类预期与内置 KeywordTicketClassifier 行为对齐（回归可重复）；
    - knowledge 检索不在本集内执行；预期文档代表“应有依据”，
      实际是否命中由 backend/run_ticket_eval.py 结合门禁验证；
    - 全部为脱敏构造文本，不包含真实客户数据。
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class TicketEvalCase(TypedDict):
    """一条固定工单评测样本。"""

    scenario: str
    text: str
    asset_id: str | None
    provided_fields: dict[str, str]
    expected_category: str
    required_fields: tuple[str, ...]
    expected_team: str
    expected_document_ids: tuple[str, ...]
    expected_human_takeover: bool
    departments: NotRequired[tuple[str, ...]]
    forbidden_document_ids: NotRequired[tuple[str, ...]]


TICKET_EVAL_VERSION = "2026-09-12-v1"

# 预期知识文档（与 backend.seed_demo 的 8 篇脱敏 IT 文档对应）
DOC_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "vpn": ("vpn-001",),
    "account": ("password-001",),
    "network": ("network-001",),
    "email": ("email-001",),
    "hardware": ("hardware-001",),
    "software": ("software-001",),
    "printer": ("printer-001",),
    "permission": ("permission-001",),
}


def _full_fields(text: str) -> dict[str, str]:
    """完整字段：V1 内各类别的必填项全部就绪（用于分类/派单类用例）。"""
    return {
        "title": text,
        "description": text,
        "requester_id": "customer-1",
        "affected_system": "example",
        "impact": "one user",
        "finance_topic": "报销",
        "request_type": "申请",
        "product_name": "example",
    }


def _missing_fields(text: str, missing: tuple[str, ...]) -> dict[str, str]:
    """字段缺失用例：从完整字段里去掉指定必填字段。"""
    fields = _full_fields(text)
    for name in missing:
        fields.pop(name, None)
    return fields


def _cat(category: str) -> str:
    return category


_VPN_ERRORS = (
    "错误码 769",
    "错误码 809",
    "一直转圈",
    "频繁掉线",
    "无法建立连接",
    "客户端安装失败",
    "证书过期",
    "认证失败",
    "分配不到 IP",
    "远程桌面不通",
)


def _vpn_cases() -> list[TicketEvalCase]:
    cases: list[TicketEvalCase] = []
    templates = (
        lambda err: f"公司 VPN 连不上，提示{err}",
        lambda err: f"VPN {err} 怎么办",
        lambda err: f"远程办公 VPN {err}",
    )
    for err in _VPN_ERRORS:
        for template in templates:
            text = template(err)
            cases.append({
                "scenario": "vpn",
                "text": text,
                "asset_id": "laptop-001",
                "provided_fields": _full_fields(text),
                "expected_category": "it.vpn",
                "required_fields": ("title", "description", "requester_id", "affected_system", "impact"),
                "expected_team": "team-it",
                "expected_document_ids": DOC_BY_CATEGORY["vpn"],
                "expected_human_takeover": False,
            })
    return cases


_ACCOUNT_TEXTS = (
    "SSO 登录失败",
    "忘记密码",
    "账号被锁定",
    "密码过期",
    "重置密码后仍无法登录",
    "账号被停用",
    "MFA 收不到验证码",
    "首次登录失败",
    "离职账号回收",
    "邮箱账号密码错误",
    "单点登录超时",
    "账号无法退出",
    "解锁账号",
    "密码策略要求",
    "多设备登录冲突",
    "账号被冒用",
    "外部门户登录失败",
    "IT 系统账号申请",
    "访客账号激活",
    "账号离职保留",
)


def _account_cases() -> list[TicketEvalCase]:
    return [
        {
            "scenario": "account",
            "text": text,
            "asset_id": None,
            "provided_fields": _full_fields(text),
            "expected_category": "it.account",
            "required_fields": ("title", "description", "requester_id", "affected_system", "impact"),
            "expected_team": "team-it",
            "expected_document_ids": DOC_BY_CATEGORY["account"],
            "expected_human_takeover": False,
        }
        for text in _ACCOUNT_TEXTS
    ]


# (text, expected_category)：与内置分类器实际行为对齐
_NETWORK_CASES_RAW = (
    ("办公区断网", "it.network"),
    ("Wi-Fi 连不上", "it.network"),
    ("网速慢", "it.network"),
    ("IP 冲突", "it.network"),
    ("DNS 无法解析", "it.network"),
    ("无法获取 IP", "it.network"),
    ("网关不通", "it.network"),
    ("交换机端口未启用", "it.network"),
    ("网线松动", "it.network"),
    ("无线信号弱", "it.network"),
    ("内网访问外网失败", "it.vpn"),
    ("外网访问内网失败", "it.vpn"),
    ("打印机走网络打印不了", "it.printer"),
    ("视频会议卡顿", "it.network"),
    ("丢包", "it.network"),
    ("延迟高", "it.network"),
    ("VPN 内网互通", "it.vpn"),
    ("网络误配置", "it.network"),
    ("代理服务器异常", "it.network"),
    ("重启路由器", "it.network"),
)


def _network_cases() -> list[TicketEvalCase]:
    cases: list[TicketEvalCase] = []
    for text, category in _NETWORK_CASES_RAW:
        sub = category.split(".", 1)[1]
        cases.append({
            "scenario": "network",
            "text": text,
            "asset_id": None,
            "provided_fields": _full_fields(text),
            "expected_category": category,
            "required_fields": ("title", "description", "requester_id", "affected_system", "impact"),
            "expected_team": "team-it",
            "expected_document_ids": DOC_BY_CATEGORY.get(sub, ()),
            "expected_human_takeover": False,
        })
    return cases


# 字段缺失场景（分类正确但必填字段不齐：应进入追问而非人工派单）
_FIELDS_MISSING_RAW = (
    ("VPN 无法连接", "it.vpn", ("affected_system",)),
    ("SSO 登录不了", "it.account", ("impact",)),
    ("办公室断网了", "it.network", ("affected_system", "impact")),
    ("电脑开不了机", "it.hardware", ("affected_system",)),
    ("打印机不出纸", "it.printer", ("impact",)),
    ("Outlook 收不到邮件", "it.email", ("affected_system",)),
    ("申请共享文件夹权限", "it.permission", ("impact",)),
    ("软件安装失败", "it.software", ("affected_system",)),
    ("申请备用机", "other", ()),
    ("邮箱容量已满", "it.email", ("impact",)),
)


def _fields_missing_cases() -> list[TicketEvalCase]:
    cases: list[TicketEvalCase] = []
    for text, category, missing in _FIELDS_MISSING_RAW:
        sub = category.split(".", 1)[1] if "." in category else None
        out_of_scope = category.startswith("finance") or category.startswith("admin") or category.startswith("product") or category in ("other",)
        cases.append({
            "scenario": "fields_missing",
            "text": text,
            "asset_id": None,
            "provided_fields": _missing_fields(text, missing),
            "expected_category": category,
            "required_fields": ("title", "description", "requester_id", "affected_system", "impact"),
            "expected_team": "team-service-desk" if out_of_scope else "team-it",
            "expected_document_ids": DOC_BY_CATEGORY.get(sub, ()) if sub else (),
            "expected_human_takeover": bool(out_of_scope),
        })
    return cases


_NO_KNOWLEDGE_RAW = (
    ("如何申请企业微信审批", "other"),
    ("公司食堂开放时间", "other"),
    ("报销系统安装包在哪里", "it.software"),
    ("会议预约系统密码", "it.account"),
    ("ERP 系统账号申请", "it.account"),
)


def _no_knowledge_cases() -> list[TicketEvalCase]:
    cases: list[TicketEvalCase] = []
    for text, category in _NO_KNOWLEDGE_RAW:
        out_of_scope = category == "other"
        cases.append({
            "scenario": "no_knowledge",
            "text": text,
            "asset_id": None,
            "provided_fields": _full_fields(text),
            "expected_category": category,
            "required_fields": ("title", "description", "requester_id", "affected_system", "impact"),
            "expected_team": "team-service-desk" if out_of_scope else "team-it",
            "expected_document_ids": (),
            "expected_human_takeover": True,
        })
    return cases


_ACL_RAW = (
    ("财务部门的收入报表权限", "it.permission"),
    ("财务文档怎么访问", "finance"),
    ("查看财务报表", "finance"),
    ("财务审批权限申请", "it.permission"),
    ("读取财务数据", "finance"),
)


def _acl_cases() -> list[TicketEvalCase]:
    cases: list[TicketEvalCase] = []
    for text, category in _ACL_RAW:
        out_of_scope = category.startswith("finance")
        cases.append({
            "scenario": "acl",
            "text": text,
            "asset_id": None,
            "provided_fields": _full_fields(text),
            "expected_category": category,
            "required_fields": ("title", "description", "requester_id", "affected_system", "impact"),
            "expected_team": "team-service-desk" if out_of_scope else "team-it",
            "expected_document_ids": (),
            "expected_human_takeover": True,
            "departments": ("it",),
            "forbidden_document_ids": ("finance-001",),
        })
    return cases


def _build_cases() -> list[TicketEvalCase]:
    return [
        *_vpn_cases(),
        *_account_cases(),
        *_network_cases(),
        *_fields_missing_cases(),
        *_no_knowledge_cases(),
        *_acl_cases(),
    ]


TICKET_EVAL_CASES: tuple[TicketEvalCase, ...] = tuple(_build_cases())


def ticket_eval_case_count() -> int:
    return len(TICKET_EVAL_CASES)


def ticket_eval_scenario_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in TICKET_EVAL_CASES:
        counts[case["scenario"]] = counts.get(case["scenario"], 0) + 1
    return counts
