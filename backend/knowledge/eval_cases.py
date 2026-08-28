"""IT 服务台检索评测集 —— 与 backend.seed_demo 的知识文档对应。

每条用例：query（员工真实问法）+ expected_document_ids（期望命中的文档，
可多文档，便于计算 Recall@k）。检索视角使用客服（internal=True），与工作台
「智能助手」一致；文档均来自 seed_demo 导入的 8 篇脱敏 IT 知识库。

用法见 backend/run_knowledge_eval.py（评测运行器）。
"""

from __future__ import annotations

from typing import TypedDict


class EvalCase(TypedDict):
    query: str
    expected_document_ids: tuple[str, ...]


EVAL_CASES: tuple[EvalCase, ...] = (
    # ---------- it.vpn（vpn-001） ----------
    {"query": "公司 VPN 连不上，提示错误码 769", "expected_document_ids": ("vpn-001",)},
    {"query": "VPN 客户端报 809 错误怎么处理", "expected_document_ids": ("vpn-001",)},
    {"query": "Windows 上怎么配置公司 VPN 连接", "expected_document_ids": ("vpn-001",)},
    {"query": "Mac 电脑添加 VPN 服务器地址填什么", "expected_document_ids": ("vpn-001",)},
    {"query": "远程办公连不上内网，VPN 一直转圈", "expected_document_ids": ("vpn-001",)},
    # ---------- it.email（email-001） ----------
    {"query": "Outlook 收不到邮件可能是什么原因", "expected_document_ids": ("email-001",)},
    {"query": "企业邮箱在手机上的 IMAP/SMTP 服务器怎么配", "expected_document_ids": ("email-001",)},
    {"query": "邮箱容量满了会影响收信吗", "expected_document_ids": ("email-001",)},
    {"query": "邮件被自动归档到垃圾箱了", "expected_document_ids": ("email-001",)},
    {"query": "首次登录企业邮箱要用什么账号密码", "expected_document_ids": ("email-001",)},
    # ---------- it.account（password-001） ----------
    {"query": "忘记密码怎么自助重置", "expected_document_ids": ("password-001",)},
    {"query": "SSO 账号被锁定多久能自动解锁", "expected_document_ids": ("password-001",)},
    {"query": "公司密码要求多少位，多久换一次", "expected_document_ids": ("password-001",)},
    {"query": "登录提示密码已过期怎么办", "expected_document_ids": ("password-001",)},
    {"query": "重置密码后还是登不进去", "expected_document_ids": ("password-001",)},
    # ---------- it.printer（printer-001） ----------
    {"query": "打印机添加设备找不到共享打印机", "expected_document_ids": ("printer-001",)},
    {"query": "打印卡纸了怎么处理", "expected_document_ids": ("printer-001",)},
    {"query": "打印报错硒鼓余量不足", "expected_document_ids": ("printer-001",)},
    {"query": "共享打印机 \\\\print-server\\printer-001 怎么装驱动", "expected_document_ids": ("printer-001",)},
    {"query": "打印机一直显示离线", "expected_document_ids": ("printer-001",)},
    # ---------- it.software（software-001） ----------
    {"query": "办公软件去哪里自助安装", "expected_document_ids": ("software-001",)},
    {"query": "安装企业软件需要管理员权限找谁开通", "expected_document_ids": ("software-001",)},
    {"query": "可以自己装未授权软件吗", "expected_document_ids": ("software-001",)},
    {"query": "部门许可证不够用怎么办", "expected_document_ids": ("software-001",)},
    {"query": "会议客户端安装失败", "expected_document_ids": ("software-001",)},
    # ---------- it.network（network-001） ----------
    {"query": "办公区断网了怎么排查", "expected_document_ids": ("network-001",)},
    {"query": "ipconfig renew 之后还是上不了网", "expected_document_ids": ("network-001",)},
    {"query": "Wi-Fi 连不上，提示密码错误", "expected_document_ids": ("network-001",)},
    {"query": "怎么判断是内网还是外网的问题", "expected_document_ids": ("network-001",)},
    {"query": "办公区无线信号弱怎么办", "expected_document_ids": ("network-001",)},
    # ---------- it.hardware（hardware-001） ----------
    {"query": "笔记本开不了机怎么报修", "expected_document_ids": ("hardware-001",)},
    {"query": "屏幕坏了维修要多久", "expected_document_ids": ("hardware-001",)},
    {"query": "报修工单需要提供哪些信息", "expected_document_ids": ("hardware-001",)},
    {"query": "维修期间可以申请备用机吗", "expected_document_ids": ("hardware-001",)},
    {"query": "电脑一直蓝屏重启", "expected_document_ids": ("hardware-001",)},
    # ---------- it.permission（permission-001） ----------
    {"query": "申请系统权限要走什么流程", "expected_document_ids": ("permission-001",)},
    {"query": "管理员权限需要谁审批", "expected_document_ids": ("permission-001",)},
    {"query": "权限申请表要填哪些内容", "expected_document_ids": ("permission-001",)},
    {"query": "离职员工的权限什么时候回收", "expected_document_ids": ("permission-001",)},
    {"query": "财务审批权限怎么申请", "expected_document_ids": ("permission-001",)},
    # ---------- 跨文档（症状关联两个分类） ----------
    {"query": "打印机装不上驱动，网络也断着", "expected_document_ids": ("printer-001", "network-001")},
    {"query": "电脑报修顺便申请权限开通", "expected_document_ids": ("hardware-001", "permission-001")},
    # ---------- 补充：口语/场景问法（50+ 门禁） ----------
    {"query": "办公室 Wi-Fi 连不上，一直转圈", "expected_document_ids": ("network-001",)},
    {"query": "SSO 账号被锁定进不去了", "expected_document_ids": ("password-001",)},
    {"query": "在家远程办公连不上公司内网", "expected_document_ids": ("vpn-001",)},
    {"query": "Outlook 发邮件失败提示 SMTP", "expected_document_ids": ("email-001",)},
    {"query": "软件中心装不了会议软件", "expected_document_ids": ("software-001",)},
    {"query": "申请共享文件夹的访问权限", "expected_document_ids": ("permission-001",)},
    {"query": "键盘鼠标突然没反应", "expected_document_ids": ("hardware-001",)},
    {"query": "打印机打印出来全是黑白，需要彩色", "expected_document_ids": ("printer-001",)},
    {"query": "VPN 登录后频繁掉线", "expected_document_ids": ("vpn-001",)},
    {"query": "邮箱一直提示容量已满", "expected_document_ids": ("email-001",)},
)


def eval_case_count() -> int:
    return len(EVAL_CASES)


def document_ids_by_category() -> dict[str, tuple[str, ...]]:
    """文档 ID -> 分类映射（用于按分类输出评测分项）。"""
    mapping: dict[str, str] = {
        "vpn-001": "it.vpn",
        "email-001": "it.email",
        "password-001": "it.account",
        "printer-001": "it.printer",
        "software-001": "it.software",
        "network-001": "it.network",
        "hardware-001": "it.hardware",
        "permission-001": "it.permission",
    }
    by_category: dict[str, tuple[str, ...]] = {}
    for document_id, category in mapping.items():
        by_category.setdefault(category, ())
        by_category[category] = (*by_category[category], document_id)
    return by_category
