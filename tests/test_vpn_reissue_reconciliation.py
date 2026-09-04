"""重新下发 VPN 配置文件（reissue_vpn_config）—— 阶段五「可恢复状态模型 + 补偿对账」测试。

覆盖六项生产验收：
    1. 未审批绝不执行（not_approved，零副作用）；
    2. 重复审批不重复执行（幂等，回调同键返回既有结果）；
    3. 外部超时不会被误判为失败（execution_unknown，而非 failed）；
    4. 外部成功但本地落库失败 → 可自动对账收敛（reconciliation_required → reconcile）；
    5. 工单状态 / 执行状态不再依赖单次请求完成（对账补写 workflow_operation + 工单）；
    6. 审计事件可追溯完整链路（started → approved → execution_started →
       reconciliation_required → reconciled）。

模型相关不使用真实 API key（fake audit/tickets/assets/runtime + 真实 MockVpnAdapter 数据源）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from backend.run_context import RunContext
from backend.vpn.approval import (
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
    """工单仓储桩；``fail_commit`` 置 True 时使 mark_workflow_operation_committed 抛错（模拟本地落库失败）。"""

    def __init__(self, *, initial_status=TicketStatus.IN_PROGRESS, version=0, fail_commit=False):
        self.cur_status = initial_status
        self.version = version
        self.fail_commit = fail_commit
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
        if self.fail_commit:
            raise RuntimeError("模拟本地 workflow_operation 落库失败")
        self.operation_committed_calls.append(operation_id)
        return True


class _TimeoutAdapter(MockVpnAdapter):
    """reissue_config 抛出超时：外部结果未知（可能已下发），不应误判为失败。"""

    async def reissue_config(self, **kwargs):
        raise TimeoutError("模拟外部执行超时")


class _FlakyAdapter(MockVpnAdapter):
    """首次调用：实际已下发但抛超时（结果未知）；对账重查：返回已下发真实结果。"""

    def __init__(self):
        super().__init__()
        self.first_call_raises_timeout = True

    async def reissue_config(self, **kwargs):
        if self.first_call_raises_timeout:
            self.first_call_raises_timeout = False
            # 真实外部其实已下发（幂等登记），但本请求因超时拿不到应答 → execution_unknown。
            await super().reissue_config(**kwargs)
            raise TimeoutError("外部已下发但本请求超时，结果未知")
        # 对账重查：幂等，返回权威真实结果（已 delivered/confirmed）。
        return await super().reissue_config(**kwargs)


def _runtime(*, audit, tickets, adapter, assets):
    return SimpleNamespace(audit=audit, tickets=tickets, vpn_adapter=adapter, assets=assets)


def _run_context(tenant_id="tenant-a", user_id="user-042", scopes=frozenset({"ticket:agent", "ticket:approve"})):
    return RunContext(
        run_id="run-reissue-reconcile",
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


def _start(svc, req, runtime, ctx):
    asyncio.run(svc.start(request=req, runtime=runtime, run_context=ctx))


# ---- 验收 1：未审批绝不执行，零副作用 ----
def test_execute_without_approval_never_runs():
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.IN_PROGRESS)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
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
    assert "vpn_reissue_executed" not in _event_names(audit)
    assert TicketAction.APPROVE not in tickets.transition_calls


# ---- 验收 2：重复审批不重复执行 ----
def test_repeated_approval_does_not_repeat_execution():
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL)
    adapter = MockVpnAdapter()
    runtime = _runtime(audit=audit, tickets=tickets, adapter=adapter, assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    _start(svc, req, runtime, ctx)

    async def run():
        r1 = await svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx)
        r2 = await svc.approve(request=req, approver_user_id="approver-2", runtime=runtime, run_context=ctx)
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.ok and r2.ok
    assert r2.idempotency_key == r1.idempotency_key
    executed_events = [e for e in audit.events if e[0] == "vpn_reissue_executed"]
    assert len(executed_events) == 1  # 只执行一次


# ---- 验收 3：外部超时不会被误判为失败 → execution_unknown（可对账） ----
def test_external_timeout_is_execution_unknown_not_failed():
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=_TimeoutAdapter(), assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    _start(svc, req, runtime, ctx)

    async def run():
        return await svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx)

    result = asyncio.run(run())
    # 关键：超时是「结果未知」，不是「失败」。
    assert result.ok is False
    assert result.status == ApprovalStatus.EXECUTION_UNKNOWN
    assert result.error_code == "execution_unknown"
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.EXECUTION_UNKNOWN
    assert svc.registry.get_status(req.idempotency_key) != ApprovalStatus.FAILED
    names = _event_names(audit)
    assert "vpn_reissue_execution_started" in names
    assert "vpn_reissue_execution_unknown" in names
    assert "vpn_reissue_failed" not in names
    # 对账扫描应识别该键。
    assert req.idempotency_key in svc.registry.scan_reconcilable()


# ---- 验收 5 的组成：对账能把「结果未知」收敛为「已确认」（外部实际已交付） ----
def test_reconcile_resolves_execution_unknown_to_confirmed():
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL)
    adapter = _FlakyAdapter()
    runtime = _runtime(audit=audit, tickets=tickets, adapter=adapter, assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    _start(svc, req, runtime, ctx)

    async def run():
        await svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx)
        return await svc.reconcile(idempotency_key=req.idempotency_key, runtime=runtime, run_context=ctx)

    reconciler = asyncio.run(run())
    assert reconciler["reconciled"] is True
    assert reconciler["status"] == ApprovalStatus.CONFIRMED.value
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.CONFIRMED
    assert req.idempotency_key in tickets.operation_committed_calls
    assert req.idempotency_key not in svc.registry.scan_reconcilable()  # 已收敛
    assert "vpn_reissue_reconciled" in _event_names(audit)


# ---- 验收 4：外部成功但本地落库失败 → reconciliation_required → 可自动对账 ----
def test_local_commit_failure_marks_reconciliation_required_then_reconcile():
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL, fail_commit=True)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    _start(svc, req, runtime, ctx)

    async def run():
        return await svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx)

    result = asyncio.run(run())
    # 外部已下发成功（ok=True / confirmed），但本地落库失败 → 不误判为成功终态。
    assert result.ok is True
    assert result.confirmed is True
    assert result.status == ApprovalStatus.CONFIRMED
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.RECONCILIATION_REQUIRED
    assert req.idempotency_key not in tickets.operation_committed_calls  # 本地未提交
    assert "vpn_reissue_reconciliation_required" in _event_names(audit)
    # 工单仍未迁移（停留在 AWAITING_APPROVAL），等待对账补写。
    assert tickets.cur_status == TicketStatus.AWAITING_APPROVAL

    # 本地落库失败是瞬态的：恢复后对账即可补写 committed + 工单迁移。
    tickets.fail_commit = False

    async def reconcile():
        return await svc.reconcile(idempotency_key=req.idempotency_key, runtime=runtime, run_context=ctx)

    out = asyncio.run(reconcile())
    assert out["reconciled"] is True
    assert out["status"] == ApprovalStatus.CONFIRMED.value
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.CONFIRMED
    assert req.idempotency_key in tickets.operation_committed_calls  # 对账补写 committed
    assert tickets.cur_status == TicketStatus.IN_PROGRESS  # 工单状态被补写（AWAITING_APPROVAL→IN_PROGRESS）
    assert "vpn_reissue_reconciled" in _event_names(audit)


# ---- 验收 5：状态不依赖单次请求完成（对账把「未完成单次请求」的状态补全） ----
def test_state_does_not_depend_on_single_request_for_reconcile():
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL, fail_commit=True)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    _start(svc, req, runtime, ctx)
    asyncio.run(svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx))

    # 单次请求后：状态停留在 reconciliation_required（未 committed、工单未迁移）。
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.RECONCILIATION_REQUIRED
    assert tickets.cur_status == TicketStatus.AWAITING_APPROVAL
    assert req.idempotency_key not in tickets.operation_committed_calls

    # 第二次独立请求（reconcile）不依赖第一次请求即可把状态补全到定态。
    tickets.fail_commit = False
    asyncio.run(svc.reconcile(idempotency_key=req.idempotency_key, runtime=runtime, run_context=ctx))
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.CONFIRMED
    assert tickets.cur_status == TicketStatus.IN_PROGRESS
    assert req.idempotency_key in tickets.operation_committed_calls


# ---- 验收 6：审计事件可追溯完整链路（start→approve→execution_started→reconcile末态） ----
def test_audit_traces_full_chain_through_reconcile():
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL, fail_commit=True)
    runtime = _runtime(audit=audit, tickets=tickets, adapter=MockVpnAdapter(), assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    _start(svc, req, runtime, ctx)
    asyncio.run(svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx))
    tickets.fail_commit = False
    asyncio.run(svc.reconcile(idempotency_key=req.idempotency_key, runtime=runtime, run_context=ctx))

    names = _event_names(audit)
    for expected in (
        "vpn_reissue_started",
        "vpn_reissue_approved",
        "vpn_reissue_execution_started",
        "vpn_reissue_reconciliation_required",
        "vpn_reissue_reconciled",
    ):
        assert expected in names, f"缺审计事件 {expected}"
    # 完整链路：每个事件都关联 tenant/user/ticket/action=/idempotency_key。
    for (event, _status, payload) in audit.events:
        if event.startswith("vpn_reissue"):
            assert payload.get("tenant_id") == req.tenant_id
            assert payload.get("user_id") == req.user_id
            assert payload.get("ticket_id") == req.ticket_id
            assert payload.get("action") == "reissue_vpn_config"
            assert payload.get("idempotency_key") == req.idempotency_key


# ---- 验收 5 的批量收敛：reconcile_all 扫全部 execution_unknown 键并收敛到定态 ----
def test_reconcile_all_sweeps_scan_reconcilable_to_terminal():
    """reconcile_all 应扫描全部可对账键并逐一收敛，不依赖单次请求完成状态。"""
    audit = _FakeAudit()
    tickets = _FakeTickets(initial_status=TicketStatus.AWAITING_APPROVAL)
    # _FlakyAdapter：首次调用超时（外部已下发但结果未知→execution_unknown），对账重查返回真实结果。
    adapter = _FlakyAdapter()
    runtime = _runtime(audit=audit, tickets=tickets, adapter=adapter, assets=_FakeAssets())
    ctx = _run_context()
    svc = VpnReissueService(registry=ReissueRegistry())
    req = _request()
    _start(svc, req, runtime, ctx)

    # 审批后首请求超时 → execution_unknown，且被 scan_reconcilable 识别。
    asyncio.run(svc.approve(request=req, approver_user_id="approver-1", runtime=runtime, run_context=ctx))
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.EXECUTION_UNKNOWN
    assert req.idempotency_key in svc.registry.scan_reconcilable()

    # reconcile_all 批量收敛：扫描该键并把它从 execution_unknown 收敛为 confirmed，且移出可对账集合。
    async def sweep():
        return await svc.reconcile_all(runtime=runtime, run_context=ctx)

    results = asyncio.run(sweep())
    assert len(results) == 1
    assert results[0]["reconciled"] is True
    assert results[0]["status"] == ApprovalStatus.CONFIRMED.value
    assert svc.registry.get_status(req.idempotency_key) == ApprovalStatus.CONFIRMED
    assert req.idempotency_key not in svc.registry.scan_reconcilable()  # 已收敛（不依赖单次请求完成）
    assert req.idempotency_key in tickets.operation_committed_calls  # workflow_operation 已补写
    assert "vpn_reissue_reconciled" in _event_names(audit)
