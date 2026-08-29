"""中文检索分词 —— jieba + 自定义 IT 词典。

查询与文档入库必须使用同一个 tokenizer：知识块写入 search_text 时用
tokenize_for_search(content)，检索时用 tokenize_for_search(query)，保证
两侧分词一致（不能一边分词、一边不分词）。

规则：
- 英文缩写（VPN / SSO / MFA / IPsec）、数字错误码（769 / 809）保持为独立 token；
- IT 组合词（企业微信 / 软件中心 / 权限申请 / 备用机）优先合并；
- 其余中文按 jieba 默认词典切分，token 以空格连接，供 plainto_tsquery 使用。
"""

from __future__ import annotations

import re

import jieba

# IT 领域词典：保证这些词不被拆散，且查询/入库行为一致。
_IT_TERMS = (
    "VPN",
    "SSO",
    "MFA",
    "Outlook",
    "IMAP",
    "SMTP",
    "Wi-Fi",
    "IPsec",
    "错误码 769",
    "错误码 809",
    "企业微信",
    "钉钉",
    "打印机",
    "软件中心",
    "权限申请",
    "备用机",
    "远程办公",
    "无法连接",
    "办公网络",
    "邮箱容量",
    "共享打印机",
    "管理员权限",
    "账号锁定",
    "重置密码",
    "忘记密码",
    "收不到邮件",
    "断网",
    "开机",
    "排查",
    "离线",
    "报修",
    "卡纸",
    "硒鼓",
    "驱动",
    "许可证",
    "审批",
    "开通",
    "开不了机",
    "转圈",
    "登不进去",
    "区域故障",
    "驱动站",
    "重启",
    "报错",
    "指示灯",
)
for _term in _IT_TERMS:
    jieba.add_word(_term)

# 标点/符号统一替换为空格，避免把中文整句粘成单个 token。
_PUNCT_RE = re.compile(r"[，。；：！？、（）()\[\]【】\"'“”‘’\-—/\\.,:;!?~`@#$%^&*_+=<>|]")

# 单字停顿词与高频口语后缀：对 IT 检索无区分度，剔除可放宽 AND 匹配。
_STOPWORDS = frozenset("的了吗呢吧啊呀哦么一个是我你他她它们就都还很太更最")
_STOP_PHRASES = frozenset(
    (
        "怎么",
        "什么",
        "怎样",
        "如何",
        "怎么办",
        "为什么",
        "有没有",
        "不会",
        "需要",
        "请问",
        "一下",
        "时候",
        "是否",
    )
)

_WHITESPACE_RE = re.compile(r"\s+")


def tokenize_for_search(text: str) -> str:
    """把中文/英文混合文本切分为空格分隔的检索 token。

    输入：VPN 客户端报 809 错误怎么处理
    输出：VPN 客户端 报 809 错误 处理
    """
    if not text or not text.strip():
        return ""
    cleaned = _PUNCT_RE.sub(" ", text)
    tokens: list[str] = []
    for token in jieba.cut(cleaned, cut_all=False):
        token = token.strip()
        if not token:
            continue
        if token in _STOPWORDS or token in _STOP_PHRASES:
            continue
        tokens.append(token)
    return _WHITESPACE_RE.sub(" ", " ".join(tokens)).strip()
