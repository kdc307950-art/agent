"""阶段五：审批前重校验（pre-approval re-checks）+ 高风险强制人工 + 全链路端到端测试。

覆盖（对齐任务 Phase-5）：
    - ``recheck_pre_approval`` 六项 LIVE 重校验（a–f），每个失败项都是互不相同的错误码；
    - ``approve_reissue`` 在 PENDING→APPROVED 前执行重校验；失败则返回 ``precheck_failed``、
      审计 ``vpn_reissue_precheck_failed``、**零副作用**（状态仍 PENDING、未执行、未落终态）；
    - ``high_risk_reasons`` 的确定性映射（auth_failed / multi_user_impact 强制人工；权限变更 /
      生产环境 / 全公司影响 / 数据泄露 / 账号解锁 / 网关配置修改 阻断自动）；
    - 高风险场景在 approve 时被拒（无 ``vpn:high_risk_approve`` scope），有该 scope 才放行；
    - 全链路端到端（建议→审批→执行→确认）经 HTTP API，断言工单结束为 IN_PROGRESS。

不依赖真实 DB（fake audit/tickets/assets/runtime + 真实 MockVpnAdapter 数据源）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.run_context import RunContext
from backend.security import Principal, rate_limit_dependency
from backend.vpn.api import router as vpn_reissue_router
from backend.vpn.approval import (
    HIGH_RISK_APPROVE_SCOPE,
    ApprovalStatus,
    ReissueRegistry,
    high_risk_reasons,
    recheck_pre_approval,
)
from backend.vpn.mock_adapter import MockVpnAdapter
from backend.vpn.reissue_service import VpnReissueService, build_reissue_request
from src.my_agent.helpdesk import TicketAction, TicketStatus, transition_ticket


# ===========================================================================
# 共享桩（fake audit/assets/tickets），行为对齐既有 reissue 测试
# ===========================================================================


class _FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def record_event(self, context, event_type, status=None, payload=None):
        self.events.append((event_type, status or "", payload or {}))


class _Assets:
    """资产台账桩：可配置 owner/status/is_deleted（供 recheck 用）。"""

    def __init__(self, *, owner_user_id="user-042", status="active", is_deleted=False):
        self.owner_user_id = owner_user_id
        self.status = status
        self.is_deleted = is_deleted

    async def get(self, tenant_id, asset_id):
        return SimpleNamespace(
            asset_id=asset_id,
            owner_user_id=self.owner_user_id,
            status=self.status,
            is_deleted=self.is_deleted,
        )


class _Tickets:
    """工单仓储桩：可配置 status/version；记录迁移动作。"""

    def __init__(self, *, initial_status=TicketStatus.IN_PROGRESS, version=0):
        self.cur_status = initial_status
        self.version = version
        self.transition_calls: list[TicketAction] = []
        self.operation_started: list[str] = []
        self.operation_failed: list[tuple[str, str]] = []
        self.operation_committed: list[str] = []

    async def get(self, tenant_id, ticket_id):
        return SimpleNamespace(ticket_id=ticket_id, status=self.cur_status, version=self.version)

    async def transition(self, tenant_id, command, scopes=None):
        self.transition_calls.append(command.action)
        self.cur_status = transition_ticket(self.cur_status, command, scopes=set(scopes or ()))
        return SimpleNamespace(ticket_id=command.ticket_id, status=self.cur_status, version=self.version)

    async def start_workflow_operation(self, *, tenant_id, ticket_id, operation_id, command_type, expected_version, checkpoint_thread_id):
        self.operation_started.append(operation_id)
        return {"status": "started", "operation_id": operation_id}

    async def mark_workflow_operation_failed(self, *, tenant_id, ticket_id, operation_id, error_code):
        self.operation_failed.append((operation_id, error_code))
        return True

    async def mark_workflow_operation_committed(self, *, tenant_id, ticket_id, operation_id, result_hash):
        self.operation_committed.append(operation_id)
        return True


def _runtime(*, audit, tickets, adapter, assets):
    return SimpleNamespace(audit=audit, tickets=tickets, vpn_adapter=adapter, assets=assets)


def _run_context(tenant_id="tenant-a", user_id="user-042", scopes=frozenset({"ticket:agent", "ticket:approve"})):
    return RunContext(
        run_id="run-precheck",
        request_id="req-1",
        tenant_id=tenant_id,
        user_id=user_id,
        thread_id=f"vpn:{tenant_id}:t-1",
        scopes=scopes,
        deadline=time.time() + 60,
        allowed_tools=None,
    )


def _request(*, tenant_id="tenant-a", user_id="user-042", ticket_id="t-1", client_version="v2.5.0", reason_codes=None):
    return build_reissue_request(
        tenant_id=tenant_id,
        user_id=user_id,
        ticket_id=ticket_id,
        client_version=client_version,
        asset_id="asset-001",
        reason_codes=reason_codes,
    )


def _event_names(audit: _FakeAudit) -> list[str]:
    return [e[0] for e in audit.events]


def _adapter(*, account_status="active", account_found=True, config_found=True):
    """构造一个显式数据的 MockVpnAdapter，避免污染模块级共享默认数据。"""
    data = {
        "accounts": {"user-042": {"user_id": "user-042", "status": account_status, "found": account_found}},
        "assets": {"asset-001": {"asset_id": "asset-001", "owner_user_id": "user-042", "status": "active", "found": True}},
        "client_configs": {"user-042": {"user_id": "user-042", "version": "v2.4.1", "found": config_found}},
        "gateways": {},
        "incidents": {},
        "similar_tickets": {},
        "knowledge": [],
    }
    return MockVpnAdapter(data=data)


def _recheck(runtime, *, request=None, operation_status=ApprovalStatus.PENDING, scopes=frozenset({"ticket:approve"})):
    req = request or _request()
    return asyncio.run(
        recheck_pre_approval(
            runtime=runtime,
            tenant_id=req.tenant_id,
            request=req,
            operation_status=operation_status,
            scopes=scopes,
        )
    )


# ===========================================================================
# 1. recheck_pre_approval 六项 LIVE 重校验（每个失败项对应不同错误码）
# ===========================================================================


def test_recheck_ok_when_all_live_checks_pass():
    """全部 LIVE 校验通过 -> ok=True，fail_reasons 为空。"""
    audit = _FakeAudit()
    tickets = _Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0)
    runtime = _runtime(
        audit=audit, tickets=tickets, adapter=_adapter(), assets=_Assets()
    )
    result = _recheck(runtime)
    assert result.ok is True
    assert result.fail_reasons == []
    assert result.details["account_status"] == "active"
    assert result.details["client_config_version"] == "v2.4.1"


def test_recheck_fails_account_not_active():
    """(a) 账号非 active -> 独立错误码 account_not_active。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0),
        adapter=_adapter(account_status="suspended"),
        assets=_Assets(),
    )
    result = _recheck(runtime)
    assert result.ok is False
    assert "account_not_active" in result.fail_reasons


def test_recheck_fails_account_not_found():
    """(a) 账号记录不存在（found=False）-> account_not_active。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0),
        adapter=_adapter(account_found=False),
        assets=_Assets(),
    )
    result = _recheck(runtime)
    assert "account_not_active" in result.fail_reasons


def test_recheck_fails_asset_owner_mismatch():
    """(b) 资产归属与用户不一致 -> asset_owner_mismatch。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0),
        adapter=_adapter(),
        assets=_Assets(owner_user_id="user-other"),
    )
    result = _recheck(runtime)
    assert "asset_owner_mismatch" in result.fail_reasons


def test_recheck_fails_asset_deleted():
    """(b) 资产已软删 -> asset_deleted。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0),
        adapter=_adapter(),
        assets=_Assets(is_deleted=True),
    )
    result = _recheck(runtime)
    assert "asset_deleted" in result.fail_reasons


def test_recheck_fails_asset_retired():
    """(b) 资产状态 retired -> asset_retired。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0),
        adapter=_adapter(),
        assets=_Assets(status="retired"),
    )
    result = _recheck(runtime)
    assert "asset_retired" in result.fail_reasons


def test_recheck_fails_config_version_unavailable():
    """(c) 客户端配置版本不可读（found=False）-> config_version_unavailable。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0),
        adapter=_adapter(config_found=False),
        assets=_Assets(),
    )
    result = _recheck(runtime)
    assert "config_version_unavailable" in result.fail_reasons


def test_recheck_fails_ticket_version_mismatch():
    """(d) 工单版本变化（非预期推进）-> ticket_version_mismatch。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.IN_PROGRESS, version=99),
        adapter=_adapter(),
        assets=_Assets(),
    )
    result = _recheck(runtime)
    assert "ticket_version_mismatch" in result.fail_reasons


def test_recheck_tolerates_awaiting_approval_version_bump():
    """(d) start 的 REQUEST_APPROVAL 推进（AWAITING_APPROVAL + 版本+1）视为预期，不过。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=1),
        adapter=_adapter(),
        assets=_Assets(),
    )
    req = _request()
    req = req.model_copy(update={"expected_version": 0})
    result = _recheck(runtime, request=req)
    assert "ticket_version_mismatch" not in result.fail_reasons


def test_recheck_fails_not_pending():
    """(e) 操作已不是 PENDING（被并发推进/打断）-> not_pending。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0),
        adapter=_adapter(),
        assets=_Assets(),
    )
    result = _recheck(runtime, operation_status=ApprovalStatus.APPROVED)
    assert "not_pending" in result.fail_reasons


def test_recheck_fails_denied_scope():
    """(f) 审批人缺少 ticket:approve / chat:approve -> denied_scope。"""
    runtime = _runtime(
        audit=_FakeAudit(),
        tickets=_Tickets(initial_status=TicketStatus.AWAITING_APPROVAL, version=0),
        adapter=_adapter(),
        assets=_Assets(),
    )
    result = _recheck(runtime, scopes=frozenset({"ticket:agent"}))
    assert "denied_scope" in result.fail_reasons


# ===========================================================================
# 2. approve_reissue 在 PENDING→APPROVED 前执行重校验；失败零副作用
# ===========================================================================


def test_approve_precheck_failure_returns_precheck_failed_zero_side_effect():
    """账号在 start 后失效 -> approve 被 precheck_failed 拒绝，状态仍 PENDING、不执行、无 delivered。"""
    audit = _FakeAudit()
    tickets = _Tickets(initial_status=TicketStatus.IN_PROGRESS)
    adapter = _adapter(account_status="active")
    runtime = _runtime(audit=audit, tickets=tickets, adapter=adapter, assets=_Assets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()

    asyncio.run(svc.start(request=req, runtime=runtime, run_context=ctx))
    # start 后账号失效（LIVE 数据变化）
    adapter._data["accounts"]["user-042"]["status"] = "suspended"

    async def run():
        return await svc.approve(
            request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx
        )

    result = asyncio.run(run())
    assert result.ok is False
    assert result.error_code == "precheck_failed"
    assert "account_not_active" in result.detail["fail_reasons"]
    # 零副作用：状态仍 PENDING、未执行、未落到 delivered/confirmed、工单未 APPROVE 迁移
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.PENDING
    assert result.delivered is False
    assert result.confirmed is False
    assert "vpn_reissue_executed" not in _event_names(audit)
    assert getattr(runtime.vpn_adapter, "_reissued", {}) == {}
    assert TicketAction.APPROVE not in tickets.transition_calls
    # 审计：独立 precheck_failed 事件带 fail_reasons
    assert "vpn_reissue_precheck_failed" in _event_names(audit)
    precheck_payload = next(p for (e, s, p) in audit.events if e == "vpn_reissue_precheck_failed")
    assert "account_not_active" in precheck_payload["fail_reasons"]
    assert precheck_payload["tenant_id"] == req.tenant_id
    assert precheck_payload["ticket_id"] == req.ticket_id


# ===========================================================================
# 3. high_risk_reasons 确定性映射
# ===========================================================================


def test_high_risk_auth_failed_and_multi_user_impact_force_human():
    """fault=auth_failed / multi_user_impact -> 强制 requires_human。"""
    assert high_risk_reasons(fault="auth_failed") == ["high_risk_auth_failed"]
    assert high_risk_reasons(fault="multi_user_impact") == ["high_risk_multi_user_impact"]
    # 二者同时出现：按固定优先级排序
    assert high_risk_reasons(fault="multi_user_impact") != high_risk_reasons(fault="auth_failed")


def test_high_risk_reason_codes_block_auto():
    """reason_codes / flags 命中高风险词 -> 阻断自动（归一化 + 去重 + 定序）。"""
    assert high_risk_reasons(reason_codes=["权限变更"]) == ["high_risk_permission_change"]
    assert high_risk_reasons(reason_codes=["生产环境"]) == ["high_risk_production_env"]
    assert high_risk_reasons(reason_codes=["全公司影响"]) == ["high_risk_company_wide_impact"]
    assert high_risk_reasons(reason_codes=["数据泄露"]) == ["high_risk_data_breach"]
    assert high_risk_reasons(reason_codes=["账号解锁"]) == ["high_risk_account_unlock"]
    assert high_risk_reasons(reason_codes=["网关配置修改"]) == ["high_risk_gateway_config_modification"]
    # flags 也纳入判定
    assert high_risk_reasons(flags=["production"]) == ["high_risk_production_env"]
    # 去重 + 定序：重复触发词只出现一次
    reasons = high_risk_reasons(reason_codes=["auth_failed", "auth_failed"])
    assert reasons == ["high_risk_auth_failed"]


def test_high_risk_empty_for_normal_request():
    """正常/低风险 request（reason_codes 无高风险词）-> 空列表。"""
    req = _request(reason_codes=["gate_passed"])
    assert high_risk_reasons(request=req) == []


# ===========================================================================
# 4. 高风险 approve 被拒（无显式人工 scope）→ 有 scope 才放行
# ===========================================================================


def _high_risk_start_and_approve(*, approver_scopes):
    """start（带高风险 reason_codes）后，用给定 scope 的审批人 approve，返回结果。"""
    audit = _FakeAudit()
    tickets = _Tickets(initial_status=TicketStatus.IN_PROGRESS)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=_adapter(), assets=_Assets())
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request(reason_codes=["multi_user_impact"])
    start_ctx = _run_context()
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=start_ctx))
    tickets.cur_status = TicketStatus.AWAITING_APPROVAL
    ctx = _run_context(scopes=frozenset(approver_scopes))

    async def run():
        return await svc.approve(
            request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx
        )

    result = asyncio.run(run())
    return result, audit, svc, req, runtime


def test_high_risk_approve_denied_without_explicit_scope():
    """高风险（multi_user_impact）且无 vpn:high_risk_approve scope -> high_risk_requires_human，零执行。"""
    result, audit, svc, req, runtime = _high_risk_start_and_approve(
        approver_scopes={"ticket:approve"}
    )
    assert result.ok is False
    assert result.error_code == "high_risk_requires_human"
    assert "high_risk_multi_user_impact" in result.detail["high_risk_reasons"]
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.PENDING
    assert "vpn_reissue_executed" not in _event_names(audit)
    assert "vpn_reissue_high_risk_denied" in _event_names(audit)


def test_high_risk_approve_allowed_with_explicit_scope():
    """高风险但有 vpn:high_risk_approve scope -> 放行并执行成功（CONFIRMED）。"""
    result, _audit, svc, req, _runtime = _high_risk_start_and_approve(
        approver_scopes={"ticket:approve", HIGH_RISK_APPROVE_SCOPE}
    )
    assert result.ok is True
    assert result.status == ApprovalStatus.CONFIRMED
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.CONFIRMED


def test_high_risk_reasons_include_request_reason_codes_at_approve():
    """approve 层用 store 快照的 reason_codes 判定高风险（而非调用方入参）。"""
    audit = _FakeAudit()
    tickets = _Tickets(initial_status=TicketStatus.IN_PROGRESS)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=_adapter(), assets=_Assets())
    svc = VpnReissueService(registry=ReissueRegistry())
    req = build_reissue_request(
        tenant_id="tenant-a",
        user_id="user-042",
        ticket_id="t-1",
        client_version="v2.5.0",
        asset_id="asset-001",
        reason_codes=["data_breach"],
    )
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=_run_context()))
    tickets.cur_status = TicketStatus.AWAITING_APPROVAL

    async def run():
        # 审批人入参不携带原因，仅 scope；快照里的 reason_codes 仍是 data_breach。
        return await svc.approve(
            request=req, approver_user_id="approver-1",
            runtime=runtime, run_context=_run_context(scopes=frozenset({"ticket:approve"})),
        )

    result = asyncio.run(run())
    assert result.ok is False
    assert result.error_code == "high_risk_requires_human"
    assert "high_risk_data_breach" in result.detail["high_risk_reasons"]


# ===========================================================================
# 5. 全链路端到端（经 HTTP API）：建议→审批→执行→确认 → 工单结束 IN_PROGRESS
# ===========================================================================


def _make_app(*, tickets, audit):
    svc = VpnReissueService(registry=ReissueRegistry())
    runtime = SimpleNamespace(
        vpn_reissue=svc,
        tickets=tickets,
        audit=audit,
        vpn_adapter=_adapter(),
        assets=_Assets(),
    )
    app = FastAPI()
    app.include_router(vpn_reissue_router)
    app.state.runtime = runtime
    return app, svc


def _install_principal(app: FastAPI, principal: Principal):
    def _dep():
        return principal

    app.dependency_overrides[rate_limit_dependency] = _dep


def _principal(user_id: str, scopes: tuple[str, ...], tenant_id="tenant-a") -> Principal:
    return Principal(tenant_id=tenant_id, user_id=user_id, scopes=frozenset(scopes))


def _payload(**overrides) -> dict[str, Any]:
    base = {
        "action": "reissue_vpn_config",
        "user_id": "user-042",
        "asset_id": "asset-001",
        "ticket_id": "t-1",
        "client_version": "v2.5.0",
        "reason_codes": ["gate_passed"],
    }
    base.update(overrides)
    return base


def test_full_chain_via_api_ends_ticket_in_progress():
    """端到端：触发→审批→受控执行→确认，工单经 _domain_after_approve 由 AWAITING_APPROVAL 回 IN_PROGRESS。"""
    tickets = _Tickets(initial_status=TicketStatus.IN_PROGRESS)
    audit = _FakeAudit()
    app, _svc = _make_app(tickets=tickets, audit=audit)
    agent = _principal("user-1", ("ticket:agent",))
    approver = _principal("approver-1", ("ticket:approve",))

    with TestClient(app) as client:
        # 建议（触发审批）
        _install_principal(app, agent)
        resp = client.post("/vpn/reissue", json=_payload())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        key = body["idempotency_key"]
        assert tickets.cur_status == TicketStatus.AWAITING_APPROVAL

        # 人工审批
        _install_principal(app, approver)
        resp2 = client.post(f"/vpn/reissue/{key}/approve", json={"operation_id": key, "decision": "approve"})
        assert resp2.status_code == 200, resp2.text
        result = resp2.json()
        assert result["ok"] is True
        assert result["delivered"] is True
        assert result["confirmed"] is True
        assert result["status"] == "confirmed"

        # 工单置回 IN_PROGRESS（_domain_after_approve 执行 APPROVE 迁移）
        assert tickets.cur_status == TicketStatus.IN_PROGRESS
        assert TicketAction.APPROVE in tickets.transition_calls

        # 审计完整链路
        names = _event_names(audit)
        for expected in ("vpn_reissue_started", "vpn_reissue_approved", "vpn_reissue_executed"):
            assert expected in names, f"缺审计事件 {expected}"

        # 查询确认终态
        _install_principal(app, agent)
        resp3 = client.get(f"/vpn/reissue/{key}")
        assert resp3.status_code == 200
        assert resp3.json()["status"] == "confirmed"
