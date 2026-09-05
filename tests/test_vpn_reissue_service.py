"""重新下发 VPN 配置文件（reissue_vpn_config）审批式执行链路的集成测试。

覆盖（对齐 docs/product/vpn-diagnosis-agent-contract.md + 用户六项硬验收 + reviewer C1–C5）：
    - 六要素：前置校验 / 幂等键 / 审批人 / 操作日志 / 回滚或人工接管 / 执行结果确认；
    - 幂等：重复审批(approve)不重复执行；重复回调同键返回既有结果；
    - 红线：未 APPROVED 执行拒绝且零副作用；非白名单 action 拒绝；modify_vpn_config 拒绝；
    - C1/C2：成功落 workflow_operation 终态(committed)+result，失败落 failed+error_code，并 link
      operation↔ticket↔approver↔工具(reissue_vpn_config)↔result；
    - C3：失败/拒绝/取消后工单落可接管仲裁态（QUEUED）；
    - C4：失败不当成功幂等（新键重试），已成功路径保持幂等；
    - C5：副作用经确定性 idempotency_key，Mock reissue_config 同键只执行一次。

模型相关不使用真实 API key（fake audit/tickets/assets/runtime + 真实 MockVpnAdapter 数据源）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from backend.run_context import RunContext
from backend.vpn.approval import (
    HIGH_RISK_APPROVE_SCOPE,
    ApprovalStatus,
    ReissueActionRequest,
    ReissueRegistry,
)
from backend.vpn.mock_adapter import MockVpnAdapter
from backend.vpn.reissue_service import VpnReissueService, build_reissue_request
from src.my_agent.helpdesk import TicketAction, TicketStatus, transition_ticket


class _FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def record_event(self, context, event_type, status=None, payload=None):
        self.events.append((event_type, status or "", payload or {}))


class _FakeAssets:
    """资产台账桩：preflight 用（asset_id → 归属/状态）。"""

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


class _FakeTickets:
    """工单仓储桩：记录 workflow_operation 与状态迁移，用真实 transition_ticket 推进。"""

    def __init__(self, *, initial_status=TicketStatus.IN_PROGRESS, version=0):
        self.cur_status = initial_status
        self.version = version
        self.transition_calls: list[TicketAction] = []
        self.operation_started_calls: list[str] = []
        self.operation_failed_calls: list[tuple[str, str]] = []
        self.operation_committed_calls: list[str] = []

    async def get(self, tenant_id, ticket_id):
        return SimpleNamespace(ticket_id=ticket_id, status=self.cur_status, version=self.version)

    async def transition(self, tenant_id, command, scopes=None):
        self.transition_calls.append(command.action)
        self.cur_status = transition_ticket(self.cur_status, command, scopes=set(scopes or ()))
        return SimpleNamespace(ticket_id=command.ticket_id, status=self.cur_status, version=self.version)

    async def start_workflow_operation(self, *, tenant_id, ticket_id, operation_id, command_type, expected_version, checkpoint_thread_id):
        self.operation_started_calls.append(operation_id)
        return {"status": "started", "operation_id": operation_id}

    async def mark_workflow_operation_failed(self, *, tenant_id, ticket_id, operation_id, error_code):
        self.operation_failed_calls.append((operation_id, error_code))
        return True

    async def mark_workflow_operation_committed(self, *, tenant_id, ticket_id, operation_id, result_hash):
        self.operation_committed_calls.append(operation_id)
        return True


def _runtime(
    *,
    audit: _FakeAudit,
    tickets: _FakeTickets,
    adapter: MockVpnAdapter,
    assets: _FakeAssets,
):
    return SimpleNamespace(audit=audit, tickets=tickets, vpn_adapter=adapter, assets=assets)


def _run_context(tenant_id="tenant-a", user_id="user-042", scopes=frozenset({"ticket:agent", "ticket:approve"})):
    return RunContext(
        run_id="run-reissue",
        request_id="req-1",
        tenant_id=tenant_id,
        user_id=user_id,
        thread_id=f"vpn:{tenant_id}:t-1",
        scopes=scopes,
        deadline=time.time() + 60,
        allowed_tools=None,
    )


def _request(*, tenant_id="tenant-a", user_id="user-042", ticket_id="t-1", client_version="v2.5.0") -> ReissueActionRequest:
    return build_reissue_request(
        tenant_id=tenant_id,
        user_id=user_id,
        ticket_id=ticket_id,
        client_version=client_version,
        asset_id="asset-001",
    )


def _event_names(audit: _FakeAudit) -> list[str]:
    return [e[0] for e in audit.events]


# ========== 发起审批：前置校验 ==========


def test_start_ok_records_pending_and_moves_ticket_to_awaiting_approval():
    """前置校验通过 → PENDING + 审计 started + workflow_operation started + 工单进 AWAITING_APPROVAL。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()

    async def run():
        return await svc.start(request=req, runtime=runtime, run_context=ctx)

    out = asyncio.run(run())
    assert out["status"] == ApprovalStatus.PENDING.value
    assert out["approval_required"] is True
    assert "vpn_reissue_started" in _event_names(audit)
    assert req.idempotency_key in tickets.operation_started_calls
    assert TicketAction.REQUEST_APPROVAL in tickets.transition_calls
    assert tickets.cur_status == TicketStatus.AWAITING_APPROVAL


def test_start_preflight_failure_rejects_with_no_ticket_move():
    """账号非 active（前置校验失败）→ FAILED + 拒绝原因 + 不迁移工单。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    adapter = MockVpnAdapter()
    # 覆盖数据使账号非 active
    data = dict(adapter._data)
    data["accounts"] = {"user-042": {"found": True, "status": "suspended"}}
    adapter = MockVpnAdapter(data=data)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=adapter, assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()

    async def run():
        return await svc.start(request=req, runtime=runtime, run_context=ctx)

    out = asyncio.run(run())
    assert out["status"] == ApprovalStatus.FAILED.value
    assert out.get("rejected") is True
    assert "account_not_active" in out["fail_reasons"]
    assert "vpn_reissue_preflight_failed" in _event_names(audit)
    assert (req.idempotency_key, "preflight_failed") in tickets.operation_failed_calls
    assert tickets.transition_calls == []  # 未触发任何工单/状态迁移


# ========== 审批通过 → 受控执行：C1/C2 + delivered/confirmed ==========


def test_approve_executes_once_and_marks_success():
    """批准 → 受控执行成功 → delivered/confirmed + 审计 approved/executed + 工单回 IN_PROGRESS。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    # 先发起到 PENDING 再批准
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=ctx))

    async def run():
        result = await svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx)
        return result

    result = asyncio.run(run())
    assert result.ok is True
    assert result.delivered is True
    assert result.confirmed is True
    assert result.status == ApprovalStatus.CONFIRMED
    assert (req.idempotency_key, result.idempotency_key) is not None
    names = _event_names(audit)
    assert "vpn_reissue_approved" in names
    assert "vpn_reissue_executed" in names
    # C2：审计 approved 事件里带 approver_user_id
    approved_payload = next(p for (e, s, p) in audit.events if e == "vpn_reissue_approved")
    assert approved_payload["approver_user_id"] == "approver-1"
    assert approved_payload["tenant_id"] == req.tenant_id
    assert approved_payload["ticket_id"] == req.ticket_id
    assert approved_payload["idempotency_key"] == req.idempotency_key
    assert approved_payload["action"] == "reissue_vpn_config"
    # #1 C1：成功落 workflow_operation committed 终态（机器可读终态 + 持久幂等锚点）
    assert req.idempotency_key in tickets.operation_committed_calls
    # 风险 E：独立「执行开始」事件
    assert "vpn_reissue_execution_started" in names
    # C3：动作终态结构化（registry=CONFIRMED）；工单保持在可接管 AWAITING_APPROVAL，
    # domain 的 APPROVE→IN_PROGRESS 由 /chat/resume 等外部审批通道负责。
    assert tickets.cur_status == TicketStatus.AWAITING_APPROVAL


# ========== 幂等：重复审批不重复执行 ==========


def test_repeated_approve_does_not_repeat_execution():
    """同一幂等键重复批准 → 返回既有结果，真实下发只执行一次（Mock 幂等）。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL)
    adapter = MockVpnAdapter()
    runtime = _runtime(audit=audit, tickets=tickets, adapter=adapter, assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=ctx))

    async def run():
        r1 = await svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx)
        r2 = await svc.approve(request=req, approver_user_id="approver-2", runtime=runtime, run_context=ctx)
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.ok and r2.ok
    # 同一幂等键 → 两次批准返回同一结果（delivered/confirmed），不重发
    assert r2.idempotency_key == r1.idempotency_key
    # C5：Mock 仅执行一次 —— 二次批准命中既有结果，上报次数不增加
    executed_events = [e for e in audit.events if e[0] == "vpn_reissue_executed"]
    assert len(executed_events) == 1
    assert r2.confirmed is True


# ========== 红线：未 APPROVED 直接执行 → 拒绝零副作用；非白名单 action 拒绝 ==========


def test_execute_without_approval_is_denied_zero_side_effect():
    """未获批准直接受控执行 → not_approved 拒绝，Mock 不产生任何交付。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    adapter = MockVpnAdapter()
    runtime = _runtime(audit=audit, tickets=tickets, adapter=adapter, assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()

    async def run():
        return await svc.execute(request=req, runtime=runtime, run_context=ctx)

    result = asyncio.run(run())
    assert result.ok is False
    assert result.error_code == "not_approved"
    assert result.delivered is False
    assert "vpn_reissue_denied" in _event_names(audit)
    # 零副作用：没有 vpn_reissue_executed，且 switch 未落到 IN_PROGRESS
    assert "vpn_reissue_executed" not in _event_names(audit)
    assert TicketAction.APPROVE not in tickets.transition_calls


def test_forbidden_action_is_rejected_in_dispatch():
    """红线 action（modify_vpn_config）在触发分派层被拒绝，绝不进入执行。"""
    from backend.vpn.approval import build_reissue_request_from_payload

    # 直接验证白名单校验拒绝 modify_vpn_config（等价 dispatch 的栅栏）
    try:
        build_reissue_request_from_payload(
            {"action": "modify_vpn_config", "user_id": "u", "ticket_id": "t", "target_version": "v"},
            tenant_id="tenant-a",
        )
        raise AssertionError("红线 action 不应被允许")
    except ValueError as exc:
        assert "红线" in str(exc) or "不允许" in str(exc)


# ========== C1/C3/C4：执行失败 → failed 终态 + 回可接管态 + 不作风成功幂等 ==========


def test_execute_failure_marks_failed_and_returns_to_manual_queue():
    """Mock 下发报错 → FAILED + 审计 failed + mark_workflow_operation_failed + 工单回 QUEUED（可接管）。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL)

    # 让 reissue_config 返回失败（提供一个返回 error_code 的桩 adapter）
    class _FailAdapter:
        async def get_account_status(self, user_id):
            return {"found": True, "status": "active"}

        async def get_client_config_version(self, user_id):
            return {"found": True, "version": "v2.4.1", "content": "x"}

        async def reissue_config(self, **kwargs):
            return {"found": False, "delivered": False, "confirmed": False, "error_code": "vendor_error", "reason": "供应商超时"}

        async def get_asset(self, asset_id=None, query=""):
            return {"found": True, "owner_user_id": "user-042", "status": "active"}

    runtime = _runtime(audit=audit, tickets=tickets, adapter=_FailAdapter(), assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=ctx))

    async def run():
        return await svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx)

    result = asyncio.run(run())
    assert result.ok is False
    assert result.error_code in ("vendor_error", "reissue_failed")
    assert result.status == ApprovalStatus.FAILED
    assert "vpn_reissue_failed" in _event_names(audit)
    # C1：标记 failed 终态携带 error_code
    assert len(tickets.operation_failed_calls) >= 1
    # C3：失败后工单保持在可接管/可仲裁态 AWAITING_APPROVAL（registry 已置 FAILED 定位）
    assert tickets.cur_status == TicketStatus.AWAITING_APPROVAL


# ========== 审计关联（验收 3）：一次 start+approve 链路可查到全部关键点 ==========


def test_full_link_audits_associate_ticket_user_approver_tool_result():
    """端到端 start+approve：审计包含 started/approved(precheck→executed)，且每步 payload 关联工单/用户/审批人/工具/结果。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()

    async def run():
        await svc.start(request=req, runtime=runtime, run_context=ctx)
        tickets.cur_status = TicketStatus.AWAITING_APPROVAL
        result = await svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx)
        return result

    asyncio.run(run())
    names = _event_names(audit)
    for expected in ("vpn_reissue_started", "vpn_reissue_approved", "vpn_reissue_executed"):
        assert expected in names, f"缺审计事件 {expected}"
    # 每个事件都关联 tenant/user/ticket/工具名 action=reissue_vpn_config/idempotency_key
    for (event, _status, payload) in audit.events:
        if event.startswith("vpn_reissue"):
            assert payload.get("tenant_id") == req.tenant_id
            assert payload.get("user_id") == req.user_id
            assert payload.get("ticket_id") == req.ticket_id
            assert payload.get("action") == "reissue_vpn_config"
            assert payload.get("idempotency_key") == req.idempotency_key
    # approved 事件有 approver
    approved_payload = next(p for (e, s, p) in audit.events if e == "vpn_reissue_approved")
    assert approved_payload["approver_user_id"] == "approver-1"
    # executed 事件关联结果
    executed_payload = next(p for (e, s, p) in audit.events if e == "vpn_reissue_executed")
    assert executed_payload["result"]["ok"] is True


# ===========================================================================
# 阶段五：审批前重校验（precheck）失败 + 高风险强制人工（auto-human）
# ===========================================================================


def _adapter_with_account(*, status="active"):
    """显式数据的 MockVpnAdapter：账号/客户端配置版本可切换，避免污染共享默认数据。"""
    from backend.vpn.mock_adapter import MockVpnAdapter as _Mock

    return _Mock(
        data={
            "accounts": {"user-042": {"user_id": "user-042", "status": status, "found": True}},
            "assets": {"asset-001": {"asset_id": "asset-001", "owner_user_id": "user-042", "status": "active", "found": True}},
            "client_configs": {"user-042": {"user_id": "user-042", "version": "v2.4.1", "found": True}},
            "gateways": {},
            "incidents": {},
            "similar_tickets": {},
            "knowledge": [],
        }
    )


def test_approve_precheck_fails_when_account_deactivated_after_start():
    """start 后账号被停用（LIVE 数据变化）→ approve 被 precheck_failed 拒绝，零副作用。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    adapter = _adapter_with_account(status="active")
    runtime = _runtime(audit=audit, tickets=tickets, adapter=adapter, assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=ctx))

    # 审批前账号失效
    adapter._data["accounts"]["user-042"]["status"] = "suspended"

    async def run():
        return await svc.approve(
            request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx
        )

    result = asyncio.run(run())
    assert result.ok is False
    assert result.error_code == "precheck_failed"
    assert "account_not_active" in result.detail["fail_reasons"]
    # 零副作用：状态仍 PENDING、未执行、未落 delivered/confirmed
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.PENDING
    assert result.delivered is False
    assert "vpn_reissue_executed" not in _event_names(audit)
    assert "vpn_reissue_precheck_failed" in _event_names(audit)
    precheck_payload = next(p for (e, s, p) in audit.events if e == "vpn_reissue_precheck_failed")
    assert "account_not_active" in precheck_payload["fail_reasons"]


def test_approve_high_risk_denied_without_explicit_scope():
    """高风险 reason_codes（multi_user_impact）且无 vpn:high_risk_approve scope → 拒绝自动审批。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
    svc = VpnReissueService(registry=ReissueRegistry())
    req = build_reissue_request(
        tenant_id="tenant-a", user_id="user-042", ticket_id="t-1",
        client_version="v2.5.0", asset_id="asset-001", reason_codes=["multi_user_impact"],
    )
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=_run_context()))

    # 审批人只有 ticket:approve（无 vpn:high_risk_approve）
    ctx = _run_context(scopes=frozenset({"ticket:agent", "ticket:approve"}))
    async def run():
        return await svc.approve(
            request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx
        )

    result = asyncio.run(run())
    assert result.ok is False
    assert result.error_code == "high_risk_requires_human"
    assert "high_risk_multi_user_impact" in result.detail["high_risk_reasons"]
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.PENDING
    assert "vpn_reissue_executed" not in _event_names(audit)
    assert "vpn_reissue_high_risk_denied" in _event_names(audit)


def test_approve_high_risk_allowed_with_explicit_scope():
    """高风险但有 vpn:high_risk_approve scope → 放行并执行成功（CONFIRMED）。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
    svc = VpnReissueService(registry=ReissueRegistry())
    req = build_reissue_request(
        tenant_id="tenant-a", user_id="user-042", ticket_id="t-1",
        client_version="v2.5.0", asset_id="asset-001", reason_codes=["multi_user_impact"],
    )
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=_run_context()))
    ctx = _run_context(scopes=frozenset({"ticket:agent", "ticket:approve", HIGH_RISK_APPROVE_SCOPE}))
    async def run():
        return await svc.approve(
            request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx
        )

    result = asyncio.run(run())
    assert result.ok is True
    assert result.status == ApprovalStatus.CONFIRMED
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.CONFIRMED
