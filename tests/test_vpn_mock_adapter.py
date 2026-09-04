"""MockVpnAdapter 只读 Mock 适配器单元测试（backend/vpn/mock_adapter.py）。

覆盖契约 §5：
    - 从 dict 与 json 文件路径两种方式加载数据；
    - 6 个只读查询方法返回 dict；缺数据返回 found:false；命中返回 found:true；
    - 内置默认数据覆盖三类验收样例：身份缺失 / multi_user_impact / 无证据。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.vpn import MockVpnAdapter


def _default_adapter() -> MockVpnAdapter:
    return MockVpnAdapter()


# ========== 数据加载：dict 与 json 文件路径 ==========


def test_loads_from_dict():
    data = {
        "accounts": {"user-x": {"user_id": "user-x", "status": "active", "found": True}},
        "gateways": {"gw-1": {"gateway_id": "gw-1", "region": "north", "status": "up", "found": True}},
        "assets": {"a-1": {"asset_id": "a-1", "hostname": "h1", "found": True}},
        "incidents": {"INC-1": {"incident_id": "INC-1", "status": "open", "found": True}},
        "similar_tickets": {"user-x": [{"ticket_id": "T-1", "fault": "connection_failed"}]},
        "knowledge": [{"document_id": "d-1", "title": "t", "content": "c"}],
    }
    adapter = MockVpnAdapter(data=data)

    async def run():
        return await adapter.get_account_status("user-x"), await adapter.get_asset(asset_id="a-1")

    account, asset = asyncio.run(run())
    assert account is not None
    assert asset.get("found") is True


def test_loads_from_json_file(tmp_path):
    payload = {"accounts": {"user-j": {"user_id": "user-j", "status": "active", "found": True}}}
    path = tmp_path / "vpn_data.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    adapter = MockVpnAdapter(data_path=str(path))
    result = asyncio.run(adapter.get_account_status("user-j"))
    assert result.get("found") is True
    assert result.get("status") == "active"


def test_json_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        MockVpnAdapter(data_path=str(tmp_path / "does-not-exist.json"))


def test_default_data_used_when_no_args():
    adapter = _default_adapter()
    result = asyncio.run(adapter.get_account_status("user-042"))
    assert result is not None


# ========== 6 个只读方法：命中 / 缺数据 found:false ==========


def test_get_account_status_hit():
    result = asyncio.run(_default_adapter().get_account_status("user-042"))
    assert result.get("found") is True
    assert result.get("status") == "active"
    assert "content" in result


def test_get_account_status_missing():
    result = asyncio.run(_default_adapter().get_account_status("user-ghost"))
    assert result.get("found") is False
    assert "未找到" in result["content"]


def test_get_gateway_status_hit_by_id():
    result = asyncio.run(_default_adapter().get_gateway_status(gateway_id="gw-cn-north"))
    assert result.get("found") is True
    assert result.get("status") == "up"


def test_get_gateway_status_hit_by_region():
    result = asyncio.run(_default_adapter().get_gateway_status(region="north"))
    assert result.get("found") is True


def test_get_gateway_status_missing():
    result = asyncio.run(_default_adapter().get_gateway_status(gateway_id="gw-missing"))
    assert result.get("found") is False


def test_get_asset_hit_by_id_and_query():
    adapter = _default_adapter()

    async def run():
        return await adapter.get_asset(asset_id="asset-001"), await adapter.get_asset(query="laptop")

    by_id, by_query = asyncio.run(run())
    assert by_id.get("found") is True
    assert by_query.get("found") is True


def test_get_asset_missing():
    result = asyncio.run(_default_adapter().get_asset(asset_id="asset-9xx"))
    assert result.get("found") is False


def test_get_incident_status_hit():
    result = asyncio.run(_default_adapter().get_incident_status("INC-9"))
    assert result.get("found") is True
    assert result.get("severity") == "major"


def test_get_incident_status_missing():
    result = asyncio.run(_default_adapter().get_incident_status("INC-none"))
    assert result.get("found") is False


def test_get_similar_tickets_hit():
    result = asyncio.run(_default_adapter().get_similar_tickets("user-042"))
    assert result.get("found") is True
    assert result.get("tickets")
    assert "历史相似工单" in result["content"]


def test_get_similar_tickets_filter_by_fault():
    result = asyncio.run(
        _default_adapter().get_similar_tickets("user-042", fault="connection_failed")
    )
    assert result.get("found") is True
    assert all(t["fault"] == "connection_failed" for t in result.get("tickets", []))


def test_get_similar_tickets_missing():
    result = asyncio.run(_default_adapter().get_similar_tickets("user-ghost"))
    assert result.get("found") is False


def test_search_knowledge_hit():
    result = asyncio.run(_default_adapter().search_knowledge("VPN"))
    assert result.get("found") is True
    assert result.get("evidence")


def test_search_knowledge_missing():
    result = asyncio.run(_default_adapter().search_knowledge("zzz-not-in-kb"))
    assert result.get("found") is False


# ========== 验收样例：默认数据必须覆盖三类转人工场景 ==========


def test_default_data_contains_identity_missing_sample():
    """身份缺失样例：该 user 不在账号目录，资产归属为空 → 身份无法确认。"""
    adapter = _default_adapter()
    result = asyncio.run(adapter.get_account_status("user-unknown"))
    assert result.get("found") is False


def test_default_data_contains_multi_user_impact_sample():
    """multi_user_impact 样例：存在 fault=multi_user_impact 的相似工单。"""
    adapter = _default_adapter()
    result = asyncio.run(adapter.get_similar_tickets("user-multi", fault="multi_user_impact"))
    assert result.get("found") is True
    assert all(t["fault"] == "multi_user_impact" for t in result.get("tickets", []))


def test_default_data_contains_no_evidence_sample():
    """无证据样例：资产与知识均无命中（found:false）。"""
    adapter = _default_adapter()

    async def run():
        return (
            await adapter.get_asset(query="totally-unknown-asset"),
            await adapter.search_knowledge("totally-unknown-topic"),
        )

    asset, knowledge = asyncio.run(run())
    assert asset.get("found") is False
    assert knowledge.get("found") is False


# ========== 只读约束：方法不改动内部数据 ==========


def test_methods_are_read_only():
    """查询不改变内部数据源（只读）。"""
    adapter = _default_adapter()
    before = json.dumps(adapter._data, ensure_ascii=False, default=str, sort_keys=True)

    async def run():
        await adapter.get_account_status("user-042")
        await adapter.get_gateway_status(gateway_id="gw-cn-north")
        await adapter.get_asset(asset_id="asset-001")
        await adapter.get_incident_status("INC-9")
        await adapter.get_similar_tickets("user-042")
        await adapter.search_knowledge("VPN")

    asyncio.run(run())
    after = json.dumps(adapter._data, ensure_ascii=False, default=str, sort_keys=True)
    assert before == after
