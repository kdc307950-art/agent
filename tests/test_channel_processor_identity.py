"""渠道入站事件身份上下文提取（Day 4）单元测试。

覆盖：payload 携带 departments / asset_id 时透传到受理图；
缺失时返回空部门 + 无资产（后续由受理图收紧权限并转人工）。
"""

from backend.channel_adapters import NormalizedChannelEvent
from backend.channel_processor import _event_identity


def _event(payload):
    return NormalizedChannelEvent(
        tenant_id="tenant-a",
        channel="wecom",
        external_event_id="evt-1",
        external_ticket_id=None,
        requester_id="u-1",
        title="VPN",
        content="cannot connect",
        payload=payload,
    )


def test_event_identity_extracts_departments_and_asset():
    requester, departments, asset_id = _event_identity(
        _event({"departments": ["it"], "asset_id": "laptop-1"})
    )
    assert requester == "u-1"
    assert departments == ["it"]
    assert asset_id == "laptop-1"


def test_event_identity_missing_fields_are_empty_tightened():
    requester, departments, asset_id = _event_identity(_event({}))
    assert requester == "u-1"
    assert departments == []
    assert asset_id is None
