import asyncio

from src.my_agent.helpdesk import KeywordTicketClassifier, TicketCategory


def classify(text, fields=None):
    return asyncio.run(KeywordTicketClassifier().classify(text, fields or {}))


def test_vpn_text_classifies_to_it_vpn_with_confidence():
    result = classify("公司 VPN 无法连接外网")
    assert result.category == TicketCategory.IT
    assert result.subcategory == "vpn"
    assert result.confidence >= 0.5
    assert result.needs_human_review is False


def test_account_text_classifies_to_it_account():
    result = classify("SSO 登录密码被锁定")
    assert result.category == TicketCategory.IT
    assert result.subcategory == "account"


def test_ambiguous_or_unknown_text_low_confidence_forces_review():
    tied = classify("报销和产品都有问题")
    assert tied.needs_human_review is True
    assert tied.confidence <= 0.5

    unknown = classify("我需要一些帮助")
    assert unknown.category == TicketCategory.OTHER
    assert unknown.confidence < 0.5
    assert unknown.needs_human_review is True


def test_all_it_subcategories_are_recognized():
    samples = {
        "vpn": "vpn 连不上",
        "account": "账号被锁定",
        "network": "办公室断网",
        "email": "outlook 收不到邮件",
        "hardware": "显示器不亮",
        "software": "软件安装失败",
        "printer": "打印机不出纸",
        "permission": "申请共享文件夹权限",
    }
    for expected, text in samples.items():
        result = classify(text)
        assert result.category == TicketCategory.IT, text
        assert result.subcategory == expected, text
