"""渠道身份目录加固（Day 3-4）单元测试。

覆盖：
    - 事件 payload 中的 departments / asset_id 一律忽略；
    - 无目录映射 -> 空部门 + 空资产 + identity_missing=True；
    - 目录映射生效 -> 使用目录中的部门/资产。
"""

import asyncio
from types import SimpleNamespace

from backend.channel_adapters import NormalizedChannelEvent
from backend.channel_processor import _event_identity


class FakeChannelIdentities:
    def __init__(self, identity):
        self.identity = identity

    async def get(self, tenant_id, channel, requester_id):
        return self.identity


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


def _identity(departments=(), asset_id=None, internal=False):
    return SimpleNamespace(
        departments=tuple(departments),
        asset_id=asset_id,
        internal=internal,
        active=True,
    )


def test_event_identity_ignores_payload_and_reads_directory():
    """payload 声称 finance 部门，只要目录未登记，结果仍是空部门（伪造无效）。"""
    runtime = SimpleNamespace(channel_identities=FakeChannelIdentities(None))
    requester, departments, asset_id, missing, internal = asyncio.run(
        _event_identity(runtime, _event({"departments": ["finance"], "asset_id": "asset-1"}))
    )
    assert requester == "u-1"
    assert departments == []
    assert asset_id is None
    assert missing is True
    assert internal is False


def test_event_identity_uses_registered_mapping():
    """目录登记 finance 的用户才返回 finance 部门；payload 不再参与。"""
    runtime = SimpleNamespace(
        channel_identities=FakeChannelIdentities(_identity(["finance"], "asset-1"))
    )
    requester, departments, asset_id, missing, internal = asyncio.run(
        _event_identity(runtime, _event({"departments": ["other"]}))
    )
    assert requester == "u-1"
    assert departments == ["finance"]
    assert asset_id == "asset-1"
    assert missing is False
    assert internal is False


def test_event_identity_uses_registered_internal_flag():
    runtime = SimpleNamespace(
        channel_identities=FakeChannelIdentities(_identity(["it"], internal=True))
    )
    requester, departments, asset_id, missing, internal = asyncio.run(
        _event_identity(runtime, _event({}))
    )
    assert requester == "u-1"
    assert departments == ["it"]
    assert asset_id is None
    assert missing is False
    assert internal is True


def test_event_identity_when_repository_unavailable_is_tightened():
    runtime = SimpleNamespace(channel_identities=None)
    requester, departments, asset_id, missing, internal = asyncio.run(
        _event_identity(runtime, _event({}))
    )
    assert requester == "u-1"
    assert departments == []
    assert asset_id is None
    assert missing is True
    assert internal is False
