"""LANGGraph VPN Diagnosis Agent — 只读 Mock VPN 适配器（隔离真实 VPN 后台）。

模块归属：backend/vpn。设计要点：
    - 抽象接口 VpnDataSource / VpnAdapter：把「真实后台调用」收口到适配器，
      后续可用真实适配器替换（满足 docs/product/vpn-v1-scope.md 第 6 节「不做真实 VPN 自动诊断」）。
    - MockVpnAdapter：可配置只读 Mock。构造入参支持 data(dict) 或 data_path(JSON 文件路径)，
      两者均可省略而使用内置确定性示例数据。
    - 6 个方法全部只读、返回简单 dict/对象；找不到时返回带 found:false 的结构。
    - 内置示例数据覆盖三类验收场景：身份缺失样例、multi_user_impact 样例、无证据样例。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

# 示例键：accounts / gateways / assets / incidents / similar_tickets / knowledge。
# 设计成 key 用 (id) 索引字典。此处用松散 dict 以支持 JSON 路径反序列化无额外依赖。
# 注入「验收样例」：
#   - identity_missing: user_id="user-unknown" 不在 accounts，且资产归属为空 → 身份缺失
#   - multi_user_impact: fault="multi_user_impact" 的相似工单与网关状态 → 群体故障
#   - no_evidence: asset 查询无命中（found:false）+ 知识无命中 → 无证据
_DEFAULT_DATA: dict[str, Any] = {
    "accounts": {
        "user-042": {
            "user_id": "user-042",
            "status": "active",
            "role": "member",
            "expires_at": "2026-12-31",
            "managed_device": True,
            "found": True,
        }
    },
    "gateways": {
        "gw-cn-north": {
            "gateway_id": "gw-cn-north",
            "region": "north",
            "status": "up",
            "load_percent": 42,
            "found": True,
        },
        "gw-ne-001": {
            "gateway_id": "gw-ne-001",
            "region": "northeast",
            "status": "degraded",
            "load_percent": 96,
            "found": True,
        },
    },
    "assets": {
        "asset-001": {
            "asset_id": "asset-001",
            "hostname": "laptop-001",
            "name": "laptop-001",
            "asset_type": "laptop",
            "status": "active",
            "owner_user_id": "user-042",
            "department": "engineering",
            "found": True,
        }
    },
    "incidents": {
        "INC-9": {
            "incident_id": "INC-9",
            "status": "monitoring",
            "severity": "major",
            "affected_user_count": 25,
            "found": True,
        }
    },
    # similar_tickets 按 user 归组；fault 用于 multi_user_impact 样例。
    "similar_tickets": {
        "user-042": [
            {
                "ticket_id": "T-102",
                "status": "resolved",
                "category": "it.vpn",
                "fault": "connection_failed",
                "title": "VPN 频繁掉线",
                "resolved_at": "2025-05-01T10:00:00Z",
            },
            {
                "ticket_id": "T-103",
                "status": "resolved",
                "category": "it.vpn",
                "fault": "frequent_disconnect",
                "title": "VPN 无法建立连接",
                "resolved_at": "2025-06-12T09:00:00Z",
            },
        ],
        "user-multi": [
            {
                "ticket_id": "T-200",
                "status": "open",
                "category": "it.vpn",
                "fault": "multi_user_impact",
                "title": "整个部门 VPN 同时无法连接",
                "resolved_at": None,
            }
        ],
    },
    "knowledge": [
        {
            "document_id": "vpn-001",
            "title": "VPN 连接失败排查",
            "content": "检查客户端版本、网关负载、账号与证书有效性；确认网络可达与内网路由。",
        },
        {
            "document_id": "vpn-002",
            "title": "VPN 多用户影响升级指引",
            "content": "同一区域多个用户同时故障时应升级为事件并转人工队列，不自动回复。",
        },
    ],
    # 客户端配置版本（按 user_id 关联账号）：供 get_client_config_version 只读查询，
    # 也是「重新下发配置（reissue_vpn_config）」前置校验的必要只读能力之一。
    "client_configs": {
        "user-042": {
            "user_id": "user-042",
            "version": "v2.4.1",
            "generated_at": "2026-01-15T00:00:00Z",
            "found": True,
        },
        "user-multi": {
            "user_id": "user-multi",
            "version": "v2.3.0",
            "generated_at": "2025-11-02T00:00:00Z",
            "found": True,
        },
    },
}

logger = logging.getLogger("langgraph.vpn")


class VpnDataSource(Protocol):
    """只读 VPN 数据源协议：任何真实适配器都应实现这 6 个只读查询。

    方法签名见 MockVpnAdapter；只读、无副作用、返回简单 dict/对象。
    """

    async def get_account_status(self, user_id: str) -> dict[str, Any]: ...

    async def get_gateway_status(
        self, gateway_id: str | None = None, region: str | None = None
    ) -> dict[str, Any]: ...

    async def get_asset(
        self, asset_id: str | None = None, query: str = ""
    ) -> dict[str, Any]: ...

    async def get_incident_status(self, incident_id: str) -> dict[str, Any]: ...

    async def get_similar_tickets(
        self, user_id: str, fault: str | None = None
    ) -> dict[str, Any]: ...

    async def search_knowledge(self, query: str, limit: int = 5) -> dict[str, Any]: ...

    async def get_client_config_version(self, user_id: str) -> dict[str, Any]: ...

    async def reissue_config(
        self, *, user_id: str, idempotency_key: str, target_version: str | None = None
    ) -> dict[str, Any]: ...


class VpnAdapter:
    """可替换的 VPN 适配器抽象基类（只读接口 + 受控写接口，具体实现见子类）。

    reissue_config 为受控写方法：必须带 idempotency_key，且仅由审批式执行链路
    （backend/vpn/approval.execute_approved_reissue）在获得批准后调用；实现不得在被
    未授权路径直接烧写。真实适配器实现时保持此契约即可替换 MockVpnAdapter。
    """

    async def get_account_status(self, user_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def get_gateway_status(
        self, gateway_id: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_asset(
        self, asset_id: str | None = None, query: str = ""
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_incident_status(self, incident_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def get_similar_tickets(
        self, user_id: str, fault: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def search_knowledge(self, query: str, limit: int = 5) -> dict[str, Any]:
        raise NotImplementedError

    async def get_client_config_version(self, user_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def reissue_config(
        self, *, user_id: str, idempotency_key: str, target_version: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError


class MockVpnAdapter(VpnAdapter):
    """只读 Mock 适配器：模拟 VPN 网关/账号目录/资产/历史工单/知识。

    初始化：MockVpnAdapter(data: dict | None = None, data_path: str | None = None)
    data 为 dict；data_path 为 JSON 文件路径（优先 data_path 于 data；两者皆缺省用内置示例）。
    全部方法只读；找不到时返回 {"found": False, "content": ...} 结构。
    """

    def __init__(
        self,
        *,
        data: dict[str, Any] | None = None,
        data_path: str | None = None,
    ) -> None:
        if data_path:
            self._data = self._load_json(data_path)
        elif data is not None:
            self._data = {k: dict(v) if isinstance(v, dict) else v for k, v in data.items()}
        else:
            self._data = _DEFAULT_DATA
        # 已执行的重下发登记（按 user_id 归组，幂等键 -> 结果）；仅受控执行写入。
        self._reissued: dict[str, dict[str, dict[str, Any]]] = {}

    @staticmethod
    def _load_json(data_path: str) -> dict[str, Any]:
        path = Path(data_path)
        if not path.exists():
            raise FileNotFoundError(f"MockVpnAdapter 数据文件不存在: {data_path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("MockVpnAdapter 数据文件必须是 JSON 对象")
        return payload

    # ---- 只读查询实现（全部异步、无副作用） ----

    async def get_account_status(self, user_id: str) -> dict[str, Any]:
        account = self._accounts().get(user_id)
        if account is None:
            return {
                "found": False,
                "content": f"未找到用户 {user_id} 的 VPN 账号记录",
                "user_id": user_id,
            }
        record = dict(account)
        record.setdefault("found", True)
        record["content"] = (
            f"账号 {user_id} 状态={record.get('status', 'unknown')}, "
            f"过期={record.get('expires_at', '未知')}, 角色={record.get('role', '未知')}"
        )
        return record

    async def get_gateway_status(
        self, gateway_id: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        gateways = self._gateways()
        if gateway_id and gateway_id in gateways:
            record = dict(gateways[gateway_id])
            record.setdefault("found", True)
            record["content"] = (
                f"网关 {gateway_id} region={record.get('region', '未知')} "
                f"status={record.get('status', 'unknown')}"
            )
            return record
        if region:
            matches = [g for g in gateways.values() if g.get("region") == region]
            if matches:
                record = dict(matches[0])
                record.setdefault("found", True)
                record["content"] = (
                    f"网关 {record.get('gateway_id')} region={region} "
                    f"status={record.get('status', 'unknown')}"
                )
                return record
        return {"found": False, "content": "未找到匹配的网关", "gateway_id": gateway_id, "region": region}

    async def get_asset(
        self, asset_id: str | None = None, query: str = ""
    ) -> dict[str, Any]:
        assets = self._assets()
        if asset_id and asset_id in assets:
            record = dict(assets[asset_id])
            record.setdefault("found", True)
            record["content"] = self._asset_text(record)
            return record
        keyword = (query or "").strip().lower()
        if keyword:
            matches = [
                a
                for a in assets.values()
                if keyword in " ".join(str(a.get(k) or "").lower() for k in ("asset_id", "hostname", "name", "asset_type", "owner_user_id"))
            ]
            if matches:
                record = dict(matches[0])
                record.setdefault("found", True)
                record["content"] = self._asset_text(record)
                return record
        return {"found": False, "content": "未找到匹配的资产", "asset_id": asset_id, "query": query}

    async def get_incident_status(self, incident_id: str) -> dict[str, Any]:
        incident = self._incidents().get(incident_id)
        if incident is None:
            return {"found": False, "content": f"未找到事件 {incident_id}", "incident_id": incident_id}
        record = dict(incident)
        record.setdefault("found", True)
        record["content"] = (
            f"事件 {incident_id} status={record.get('status', 'unknown')} "
            f"severity={record.get('severity', 'unknown')}"
        )
        return record

    async def get_similar_tickets(
        self, user_id: str, fault: str | None = None
    ) -> dict[str, Any]:
        tickets = list(self._similar_tickets().get(user_id, []))
        if fault:
            tickets = [t for t in tickets if t.get("fault") == fault]
        if not tickets:
            return {"found": False, "content": "没有找到相似的历史工单", "user_id": user_id, "fault": fault}
        lines = [
            f"- #{t['ticket_id']} [{t.get('status', '?')}] {t.get('category', '?')} "
            f"| {t.get('title', '')}"
            f"（{t.get('resolved_at') or '未解决'}）"
            for t in tickets[:20]
        ]
        return {
            "found": True,
            "content": "历史相似工单：\n" + "\n".join(lines),
            "tickets": tickets,
            "user_id": user_id,
            "fault": fault,
        }

    async def search_knowledge(self, query: str, limit: int = 5) -> dict[str, Any]:
        keyword = (query or "").strip().lower()
        hits: list[dict[str, Any]] = []
        if keyword:
            for doc in self._knowledge():
                haystack = " ".join(
                    str(doc.get(k) or "").lower()
                    for k in ("document_id", "title", "content")
                )
                if keyword in haystack:
                    hits.append(doc)
        hits = hits[: max(1, min(limit, 20))]
        if not hits:
            return {"found": False, "content": "知识库未找到相关内容", "evidence": []}
        lines = [
            f"- [{d['document_id']}] {d['title']}: {d.get('content', '')[:80]}"
            for d in hits
        ]
        return {
            "found": True,
            "content": "知识库命中：\n" + "\n".join(lines),
            "evidence": hits,
        }

    async def get_client_config_version(self, user_id: str) -> dict[str, Any]:
        """查询某用户的客户端配置版本（只读；供 reissue 前置校验）。"""
        cfg = self._client_configs().get(user_id)
        if cfg is None:
            return {
                "found": False,
                "content": f"未找到用户 {user_id} 的客户端配置版本",
                "user_id": user_id,
            }
        record = dict(cfg)
        record.setdefault("found", True)
        record["content"] = (
            f"客户端配置版本 user={user_id} version={record.get('version', '未知')} "
            f"生成于 {record.get('generated_at', '未知')}"
        )
        return record

    async def reissue_config(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        target_version: str | None = None,
    ) -> dict[str, Any]:
        """受控写：重新下发客户端配置（只写操作，隔离真实后台）。

        调用约束（由 backend/vpn/approval.execute_approved_reissue 强制，不信任调用方）：
            - 必须带 idempotency_key：缺失直接拒绝；
            - 同一 user_id + 幂等键重复调用返回既有结果，不重复执行（幂等）；
            - 该用户必须有 client_config 记录（found:true），否则拒绝。
        """
        if not idempotency_key:
            return {
                "found": False,
                "delivered": False,
                "confirmed": False,
                "error_code": "missing_idempotency_key",
                "reason": "缺少幂等键，拒绝执行",
                "user_id": user_id,
            }
        cfg = self._client_configs().get(user_id)
        if cfg is None:
            return {
                "found": False,
                "delivered": False,
                "confirmed": False,
                "error_code": "account_config_not_found",
                "reason": "该用户无客户端配置记录",
                "user_id": user_id,
            }
        executed = self._reissued.setdefault(user_id, {})
        if idempotency_key in executed:
            return dict(executed[idempotency_key])
        new_version = target_version or cfg.get("version") or "v-next"
        state = {
            "found": True,
            "delivered": True,
            "confirmed": True,
            "version": new_version,
            "target_version": new_version,
            "user_id": user_id,
            "idempotency_key": idempotency_key,
            "reason": "配置已重新下发并确认",
        }
        executed[idempotency_key] = state
        record = dict(cfg)
        record["version"] = new_version
        self._client_configs()[user_id] = record
        return dict(state)

    # ---- 底层数据访问（只读） ----

    def _accounts(self) -> dict[str, Any]:
        return self._data.get("accounts") or {}

    def _gateways(self) -> dict[str, Any]:
        return self._data.get("gateways") or {}

    def _assets(self) -> dict[str, Any]:
        return self._data.get("assets") or {}

    def _incidents(self) -> dict[str, Any]:
        return self._data.get("incidents") or {}

    def _similar_tickets(self) -> dict[str, Any]:
        return self._data.get("similar_tickets") or {}

    def _knowledge(self) -> list[dict[str, Any]]:
        return self._data.get("knowledge") or []

    def _client_configs(self) -> dict[str, Any]:
        return self._data.get("client_configs") or {}

    @staticmethod
    def _asset_text(record: dict[str, Any]) -> str:
        return (
            f"资产：{record.get('asset_id')} {record.get('hostname') or record.get('name') or ''} "
            f"({record.get('asset_type')}) 状态={record.get('status')} "
            f"归属={record.get('owner_user_id') or '无'}"
        )
