"""hybrid_holdout 评测集 —— 冻结、独立于 seed_eval 的检索策略评估集。

与 seed_eval（backend/knowledge/eval_cases.py，开发期回归集）的区别：

- 本集**冻结**（HOLDOUT_VERSION），不参与检索策略调参；任何变更必须更新版本号
  并记录理由（维护者建议与检索策略调参者分离）。
- 覆盖 seed 集未覆盖的难点：口语改写、跨文档、多部门 ACL、低频错误码、
  近义词、无答案问题。
- 无答案（expected_none）与 ACL 隔离（forbidden_document_ids）用例**单独统计**，
  不混入 Top1 / Recall@k / MRR 召回指标（见 run_knowledge_eval 的用例分类）。

用例字段：
- query：员工真实问法（必填）
- expected_document_ids：期望召回的文档（计入召回指标；与 expected_none 互斥）
- expected_none：无答案用例，期望零命中（单独统计正确拒绝率）
- forbidden_document_ids：不应命中的文档（ACL 隔离验证，单独统计泄露数）
- principal_departments：评测主体的部门（默认空集；ACL 用例显式指定）

依赖的演示知识库（backend/seed_demo.py，2026-08-30 起）：
- 8 篇 public 文档（vpn/email/account/printer/software/network/hardware/permission）
- finance-001（restricted，仅 finance 部门可见）——部门 ACL 用例的数据基础
"""

from __future__ import annotations

from typing import NotRequired, TypedDict

# 冻结版本号：变更 holdout 集时必须递增并记录变更理由
HOLDOUT_VERSION = "2026-08-30-v1"


class HoldoutCase(TypedDict):
    query: str
    expected_document_ids: NotRequired[tuple[str, ...]]
    expected_none: NotRequired[bool]
    forbidden_document_ids: NotRequired[tuple[str, ...]]
    principal_departments: NotRequired[tuple[str, ...]]


HOLDOUT_CASES: tuple[HoldoutCase, ...] = (
    # ---------- 口语改写（与 seed_eval 的 10 条不重复，更口语/场景化） ----------
    {"query": "公司网突然没了，重启路由器也没用", "expected_document_ids": ("network-001",)},
    {"query": "打出来的纸全是条纹，咋整", "expected_document_ids": ("printer-001",)},
    {"query": "邮箱炸了，一直发不出去", "expected_document_ids": ("email-001",)},
    {"query": "电脑卡成狗，想换一台", "expected_document_ids": ("hardware-001",)},
    {"query": "我想开个权限，找谁", "expected_document_ids": ("permission-001",)},
    # ---------- 跨文档（一个工单关联两个分类） ----------
    {
        "query": "邮箱容量满了，同时笔记本开不了机",
        "expected_document_ids": ("email-001", "hardware-001"),
    },
    {
        "query": "出差回来报销发票，顺便换个密码",
        "expected_document_ids": ("finance-001", "password-001"),
    },
    # ---------- 多部门 ACL（finance-001 为 restricted，仅 finance 可见） ----------
    {
        "query": "发票怎么报销",
        "expected_document_ids": ("finance-001",),
        "principal_departments": ("finance",),
    },
    {
        # 非 finance 主体：不应泄露 restricted 文档（ACL 隔离，单独统计）
        "query": "发票怎么报销",
        "forbidden_document_ids": ("finance-001",),
        "principal_departments": ("it",),
    },
    # ---------- 低频错误码（文档正文未出现该错误码，依赖语义/关联词） ----------
    {"query": "VPN 报 691 用户名或密码不正确", "expected_document_ids": ("vpn-001",)},
    {"query": "VPN 错误 800，无法建立连接", "expected_document_ids": ("vpn-001",)},
    {"query": "打印机报 001-999 卡纸错误", "expected_document_ids": ("printer-001",)},
    # ---------- 近义词 / 口语近义（词面不重叠的难点） ----------
    {"query": "插了网线还是不通", "expected_document_ids": ("network-001",)},
    {"query": "打不出字", "expected_document_ids": ("printer-001",)},
    {"query": "重置后依然无法登录", "expected_document_ids": ("password-001",)},
    # ---------- 无答案（知识库无对应内容，应转人工；单独统计正确拒绝） ----------
    # 注意：措辞须与库内文档零词面重叠（如避免"申请/公司"这类泛词），
    # 否则 lexical 会误召回，测不到"无答案正确拒绝"本身。
    {"query": "年假有几天", "expected_none": True},
    {"query": "体检去哪家医院做", "expected_none": True},
    {"query": "加班费怎么算", "expected_none": True},
    {"query": "工牌丢了怎么补办", "expected_none": True},
)


def holdout_case_count() -> int:
    return len(HOLDOUT_CASES)
