"""VPN 真实防护层集成断言（闭环评审 D4）。

背景（D4 缺口）：
    run_vpn_eval（static/db 词法检索）只做**结构校验**，不触发 tool_governance / repository
    租户隔离 / FORBIDDEN_COMMANDS / approval 审批门禁等**真实防护**。本文件在真实
    PostgreSQL/Redis 测试栈（compose.test）上补齐这些断言，证明「越权/未审批被拒」。

范围（仅真实防护层；不触发真实 LLM）：
    1. 跨租户/未启用租户的工具调用被 tool_governance 拒绝（denied）且不执行，并落真实审计；
    2. 未审批的 reissue 不产生副作用：start 后仅 PENDING，未 approve 前 execute/再次触发被拒，
       不落 delivered/confirmed、不下发（MockVpnAdapter 无副作用登记）；
    3. 审批拒绝路径：approve/reject 幂等、REJECTED 终态、工单状态回可接管（domain 迁移正确）、
       缺 ticket:approve scope / 跨租户被 403。

真实性强弱说明：
    - 使用真实 PG：TicketRepository / AssetRepository / AuditRepository（compose.test 55436）；
    - 使用真实 MockVpnAdapter（确定性只读/受控写数据源）；
    - 不使用 FakeTickets / FakeAssets / FakeAudit / Fake runtime；
    - 审批状态机 ReissueRegistry 为模块级内存登记表（生产即如此），此处用独立实例保证测试隔离；
    - 工单状态迁移与 workflow_operation 终态均落真实 PG。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from psycopg_pool import AsyncConnectionPool

from backend.assets import AssetRepository
from backend.assets.models import CreateAsset
from backend.audit import AuditRepository
from backend.run_context import RunContext
from backend.tickets import CreateTicket, TicketRepository
from backend.tool_governance import DEFAULT_TOOL_POLICIES, ToolGovernance
from backend.vpn.api import ApprovalDecision, ReissueStartPayload
from backend.vpn.approval import ApprovalStatus, ReissueRegistry, build_reissue_request_from_payload
from backend.vpn.diagnosis import (
    CustomerActionStatus,
    VpnCustomerAction,
    VpnCustomerActionResult,
)
from backend.vpn.mock_adapter import MockVpnAdapter
from backend.vpn.reissue_service import VpnReissueService
from backend.vpn.repository import VpnDiagnosisRepository
from src.my_agent.helpdesk import ActorType, TicketAction, TicketCommand, TicketStatus

DATABASE_URL = os.getenv("TEST_DATABASE_URL", os.getenv("DATABASE_URL", "")).strip()
REDIS_URL = os.getenv("REDIS_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not REDIS_URL,
    reason="需要 TEST_DATABASE_URL/REDIS_URL（compose.test：55436/56379）",
)


REISSUE_USER = "user-042"  # MockVpnAdapter 里 active + 有 client_config 版本的用户
REISSUE_TARGET_VERSION = "v2.5.0"


async def _open_runtime() -> (
    tuple[AsyncConnectionPool, AuditRepository, TicketRepository, AssetRepository]
):
    os.environ.setdefault("DATABASE_URL", DATABASE_URL)
    from backend.migrations import setup_postgres

    await setup_postgres()
    pool = AsyncConnectionPool(
        DATABASE_URL, min_size=1, max_size=4, open=False, name="vpn-protection"
    )
    await pool.open(wait=True)
    return pool, AuditRepository(pool), TicketRepository(pool), AssetRepository(pool)


async def _create_asset(assets: AssetRepository, tenant_id: str, asset_id: str) -> Any:
    await assets.create(
        tenant_id,
        CreateAsset(
            asset_id=asset_id,
            asset_no=f"no-{asset_id}",
            asset_type="laptop",
            name=asset_id,
            hostname=asset_id,
            owner_user_id=REISSUE_USER,
        ),
    )
    return await assets.get(tenant_id, asset_id)


async def _advance_ticket_to_in_progress(
    tickets: TicketRepository, tenant_id: str, ticket_id: str
) -> Any:
    """把一份工单按 domain 状态机推进到 IN_PROGRESS（真实版本号连锁）。"""
    await tickets.create(
        tenant_id,
        CreateTicket(
            ticket_id=ticket_id,
            requester_id=REISSUE_USER,
            channel="web",
            title="VPN 重新下发配置",
            description="reissue_vpn_config",
            actor_type=ActorType.CUSTOMER,
            actor_id=REISSUE_USER,
        ),
    )
    current = await tickets.get(tenant_id, ticket_id)
    version = int(current.version)
    for action in (
        TicketAction.START_INTAKE,
        TicketAction.CLASSIFY,
        TicketAction.QUEUE,
        TicketAction.ASSIGN,
        TicketAction.START_WORK,
    ):
        current = await tickets.transition(
            tenant_id,
            TicketCommand(
                ticket_id=ticket_id,
                action=action,
                actor_type=ActorType.AGENT,
                actor_id="agent-1",
                expected_version=version,
            ),
            scopes=frozenset({"ticket:agent"}),
        )
        version = int(current.version)
    assert current.status == TicketStatus.IN_PROGRESS, current.status
    return current


def _build_runtime(tickets: TicketRepository, assets: AssetRepository, audit: AuditRepository):
    registry = ReissueRegistry()
    svc = VpnReissueService(registry=registry)
    runtime = SimpleNamespace(
        vpn_reissue=svc,
        tickets=tickets,
        assets=assets,
        audit=audit,
        vpn_adapter=MockVpnAdapter(),
    )
    return runtime, registry, svc


def _fake_request(runtime: Any) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
        url=SimpleNamespace(path="/vpn/reissue"),
    )


async def _count_agent_events(pool: AsyncConnectionPool, tenant_id: str, event_type: str) -> int:
    """按租户 + 事件类型统计 agent_events（run_id 由端点内部生成，不依赖外部值）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM agent_events WHERE tenant_id = %s AND event_type = %s",
                (tenant_id, event_type),
            )
            row = await cur.fetchone()
            return int(row[0])


def _reissue_payload(asset_id: str, ticket_id: str) -> ReissueStartPayload:
    return ReissueStartPayload(
        action="reissue_vpn_config",
        user_id=REISSUE_USER,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=REISSUE_TARGET_VERSION,
    )


async def _domain_transition(
    tickets: TicketRepository, tenant_id: str, ticket_id: str, action: TicketAction, actor_id: str
) -> None:
    """显式执行审批后/拒绝后的 domain 状态迁移（等价 API _domain_after_approve/_domain_after_reject）。"""
    current = await tickets.get(tenant_id, ticket_id)
    await tickets.transition(
        tenant_id,
        TicketCommand(
            ticket_id=ticket_id,
            action=action,
            actor_type=ActorType.APPROVER,
            actor_id=actor_id,
            expected_version=int(current.version),
            payload={"action": "reissue_vpn_config", "idempotency_key": ""},
        ),
        scopes=frozenset({"ticket:approve"}),
    )


def run_context(
    *, tenant_id: str, user_id: str, scopes: frozenset[str], ticket_id: str = "t"
) -> RunContext:
    return RunContext(
        run_id=f"ctx-{uuid4().hex[:12]}",
        request_id=uuid4().hex[:12],
        tenant_id=tenant_id,
        user_id=user_id,
        thread_id=f"vpn:{tenant_id}:{ticket_id}",
        scopes=scopes,
        deadline=asyncio.get_running_loop().time() + 60,
        allowed_tools=None,
    )


# ===========================================================================
# 1. 跨租户 / 未启用租户 / 缺少 scope 的工具调用被 tool_governance 拒绝（含真实审计落库）
# ===========================================================================


def test_cross_tenant_tool_call_denied_and_audited():
    """租户不在 tenant_allowlist -> 工具被拒（denied），不执行，审计事件落真实 PG。"""

    async def run():
        pool, audit, _tickets, _assets = await _open_runtime()
        try:
            governance = ToolGovernance(
                audit,
                policies=DEFAULT_TOOL_POLICIES,
                tenant_allowlist={"tenant-a": frozenset({"search_knowledge", "search_assets"})},
            )
            # 租户 b 不在白名单，尽管它有 ticket:agent scope
            ctx = run_context(
                tenant_id="tenant-b", user_id="user-1", scopes=frozenset({"ticket:agent"})
            )
            await audit.start_run(ctx, metadata={"label": "cross-tenant deny"})

            executed = False

            async def execute(_request):
                nonlocal executed
                executed = True
                return "should not run"

            request = SimpleNamespace(
                tool_call={
                    "name": "search_knowledge",
                    "args": {"query": "VPN"},
                    "id": "call-1",
                    "type": "tool_call",
                },
                tool=object(),
                runtime=SimpleNamespace(context=ctx),
            )
            result = await governance.awrap_tool_call(request, execute)
            events = await audit.list_events(ctx.tenant_id, ctx.run_id)
            return result, executed, events, governance.metrics
        finally:
            await pool.close()

    result, executed, events, metrics = asyncio.run(run())
    assert result.status == "error"
    assert "租户" in result.content
    assert executed is False, "未授权租户的工具不得执行"
    denials = [e for e in events if e["event_type"] == "tool_call_denied"]
    assert denials, "必须有 tool_call_denied 审计事件落库"
    assert denials[0]["payload"].get("reason") == "tenant_tool_policy"
    assert metrics.snapshot().get("tool_call_denied_total", 0) >= 1


def test_tool_denied_when_missing_scope_and_not_executed():
    """工具需要 ticket:agent scope；缺失时被拒且不执行，审计事件落真实 PG。"""

    async def run():
        pool, audit, _tickets, _assets = await _open_runtime()
        try:
            governance = ToolGovernance(audit, policies=DEFAULT_TOOL_POLICIES)
            ctx = run_context(
                tenant_id="tenant-a", user_id="user-1", scopes=frozenset({"chat:read"})
            )
            await audit.start_run(ctx, metadata={"label": "missing-scope deny"})

            executed = False

            async def execute(_request):
                nonlocal executed
                executed = True
                return "should not run"

            request = SimpleNamespace(
                tool_call={
                    "name": "search_knowledge",
                    "args": {"query": "VPN"},
                    "id": "call-2",
                    "type": "tool_call",
                },
                tool=object(),
                runtime=SimpleNamespace(context=ctx),
            )
            result = await governance.awrap_tool_call(request, execute)
            events = await audit.list_events(ctx.tenant_id, ctx.run_id)
            return result, executed, events
        finally:
            await pool.close()

    result, executed, events = asyncio.run(run())
    assert result.status == "error"
    assert "权限" in result.content
    assert executed is False
    denials = [e for e in events if e["event_type"] == "tool_call_denied"]
    assert denials and denials[0]["payload"].get("reason") == "missing_scope"


# ===========================================================================
# 2. 未审批的 reissue 不产生副作用
# ===========================================================================


def test_unapproved_reissue_no_side_effect():
    """start -> PENDING；未 approve 前 execute 被拒（not_approved），不下发、不落 delivered/confirmed。"""

    async def run():
        pool, audit, tickets, assets = await _open_runtime()
        try:
            tenant = f"tenant-{uuid4().hex}"
            ticket_id = f"ticket-{uuid4().hex[:10]}"
            asset_id = f"asset-{uuid4().hex[:8]}"
            await _create_asset(assets, tenant, asset_id)
            ticket = await _advance_ticket_to_in_progress(tickets, tenant, ticket_id)

            runtime, registry, svc = _build_runtime(tickets, assets, audit)
            req = build_reissue_request_from_payload(
                {
                    "action": "reissue_vpn_config",
                    "user_id": REISSUE_USER,
                    "asset_id": asset_id,
                    "ticket_id": ticket_id,
                    "client_version": REISSUE_TARGET_VERSION,
                },
                tenant_id=tenant,
                expected_version=ticket.version,
            )
            ctx = run_context(
                tenant_id=tenant,
                user_id="agent-1",
                scopes=frozenset({"ticket:agent"}),
                ticket_id=ticket_id,
            )
            await audit.start_run(ctx, metadata={"label": "unapproved-reissue"})

            start = await svc.start(request=req, runtime=runtime, run_context=ctx)
            assert start["status"] == ApprovalStatus.PENDING.value
            assert start.get("approval_required") is True
            assert registry.get_status(req.idempotency_key) == ApprovalStatus.PENDING

            # 工单已迁 AWAITING_APPROVAL
            after_start = await tickets.get(tenant, ticket_id)
            assert after_start.status == TicketStatus.AWAITING_APPROVAL

            # workflow_operation 已登记但未 committed
            op = await tickets.get_workflow_operation(
                tenant_id=tenant, ticket_id=ticket_id, operation_id=req.idempotency_key
            )
            assert op is not None
            assert op["status"] != "committed"

            # 未审批前执行 -> not_approved，零交付副作用
            result = await svc.execute(request=req, runtime=runtime, run_context=ctx)
            assert result.ok is False
            assert result.error_code == "not_approved"
            assert result.delivered is False
            assert result.confirmed is False
            assert getattr(runtime.vpn_adapter, "_reissued", {}) == {}, "未审批不得触发真实下发"

            # 审计：有 started，且无 executed 成功终态
            events = await audit.list_events(tenant, ctx.run_id)
            names = [e["event_type"] for e in events]
            assert "vpn_reissue_started" in names
            assert "vpn_reissue_executed" not in names
            return start, result, after_start
        finally:
            await pool.close()

    start, result, after_start = asyncio.run(run())
    assert after_start.status == TicketStatus.AWAITING_APPROVAL


# ===========================================================================
# 3. 审批拒绝路径 + 幂等 + 域迁移 + 403（scope/跨租户）
# ===========================================================================


async def _prepare_reissue_env(pool):
    audit = AuditRepository(pool)
    tickets = TicketRepository(pool)
    assets = AssetRepository(pool)
    tenant = f"tenant-{uuid4().hex}"
    ticket_id = f"ticket-{uuid4().hex[:10]}"
    asset_id = f"asset-{uuid4().hex[:8]}"
    await _create_asset(assets, tenant, asset_id)
    ticket = await _advance_ticket_to_in_progress(tickets, tenant, ticket_id)
    runtime, registry, svc = _build_runtime(tickets, assets, audit)
    return tickets, assets, audit, runtime, registry, svc, tenant, ticket_id, asset_id, ticket


def test_reject_path_terminal_and_ticket_recoverable():
    """审批拒绝 -> REJECTED 终态 + workflow_operation failed + 工单回 IN_PROGRESS（可继续处理）+ 403。"""

    async def run():
        pool, _audit, _tickets, _assets = await _open_runtime()
        try:
            tickets, assets, audit, runtime, registry, svc, tenant, ticket_id, asset_id, ticket = (
                await _prepare_reissue_env(pool)
            )
            req = build_reissue_request_from_payload(
                {
                    "action": "reissue_vpn_config",
                    "user_id": REISSUE_USER,
                    "asset_id": asset_id,
                    "ticket_id": ticket_id,
                    "client_version": REISSUE_TARGET_VERSION,
                },
                tenant_id=tenant,
                expected_version=ticket.version,
            )
            # service.start（自建 run_context 并注册，审计事件才能落真实 PG）
            agent_ctx = run_context(
                tenant_id=tenant,
                user_id="agent-1",
                scopes=frozenset({"ticket:agent"}),
                ticket_id=ticket_id,
            )
            await audit.start_run(agent_ctx, metadata={"label": "reject-start"})
            start = await svc.start(request=req, runtime=runtime, run_context=agent_ctx)
            key = start["idempotency_key"]
            assert start["status"] == ApprovalStatus.PENDING.value
            assert registry.get_status(key) == ApprovalStatus.PENDING

            # 缺 ticket:approve scope -> 403（端点校验，发生在任何审计写之前）
            from backend.security import Principal
            from backend.vpn.api import approve_reissue as approve_ep

            agent_p = Principal(
                tenant_id=tenant, user_id="agent-1", scopes=frozenset({"ticket:agent"})
            )
            with pytest.raises(HTTPException) as exc:
                await approve_ep(
                    operation_id=key,
                    request=_fake_request(runtime),
                    payload=ApprovalDecision(operation_id=key, decision="approve"),
                    principal=agent_p,
                )
            assert exc.value.status_code == 403

            # 跨租户查询（key 内嵌租户与主体不一致）-> 403
            from backend.vpn.api import get_reissue as get_ep

            other = Principal(
                tenant_id="tenant-other", user_id="x", scopes=frozenset({"ticket:agent"})
            )
            with pytest.raises(HTTPException) as exc2:
                await get_ep(operation_id=key, request=_fake_request(runtime), principal=other)
            assert exc2.value.status_code == 403

            # 审批拒绝 -> REJECTED
            approver_ctx = run_context(
                tenant_id=tenant,
                user_id="approver-1",
                scopes=frozenset({"ticket:approve"}),
                ticket_id=ticket_id,
            )
            await audit.start_run(approver_ctx, metadata={"label": "reject"})
            rejected = await svc.reject(
                request=req,
                runtime=runtime,
                run_context=approver_ctx,
                approver_user_id="approver-1",
                reason="审批拒绝",
            )
            assert rejected.status == ApprovalStatus.REJECTED
            assert registry.get_status(key) == ApprovalStatus.REJECTED

            # service.reject 不改工单（保持 AWAITING_APPROVAL）；domain REJECT 迁移由审批通道执行
            mid = await tickets.get(tenant, ticket_id)
            assert mid.status == TicketStatus.AWAITING_APPROVAL

            # 显式执行 domain REJECT 迁移（AWAITING_APPROVAL -> IN_PROGRESS，等价 API _domain_after_reject）
            await _domain_transition(tickets, tenant, ticket_id, TicketAction.REJECT, "approver-1")
            after = await tickets.get(tenant, ticket_id)
            assert after.status == TicketStatus.IN_PROGRESS

            # workflow_operation 终态 failed
            op = await tickets.get_workflow_operation(
                tenant_id=tenant, ticket_id=ticket_id, operation_id=key
            )
            assert op is not None and op["status"] == "failed"

            # 审计：拒绝事件已落库
            rejected_count = await _count_agent_events(pool, tenant, "vpn_reissue_rejected")
            assert rejected_count == 1

            # 未产生任何下发副作用
            assert getattr(runtime.vpn_adapter, "_reissued", {}) == {}
            return start, rejected, after
        finally:
            await pool.close()

    start, rejected, after = asyncio.run(run())
    assert after.status == TicketStatus.IN_PROGRESS


def test_approve_path_power_idempotent_and_ticket_recoverable():
    """审批通过 -> CONFIRMED 终态 + workflow_operation committed + 工单回 IN_PROGRESS + 重复审批幂等。"""

    async def run():
        pool, _audit, _tickets, _assets = await _open_runtime()
        try:
            tickets, assets, audit, runtime, registry, svc, tenant, ticket_id, asset_id, ticket = (
                await _prepare_reissue_env(pool)
            )
            req = build_reissue_request_from_payload(
                {
                    "action": "reissue_vpn_config",
                    "user_id": REISSUE_USER,
                    "asset_id": asset_id,
                    "ticket_id": ticket_id,
                    "client_version": REISSUE_TARGET_VERSION,
                },
                tenant_id=tenant,
                expected_version=ticket.version,
            )
            agent_ctx = run_context(
                tenant_id=tenant,
                user_id="agent-1",
                scopes=frozenset({"ticket:agent"}),
                ticket_id=ticket_id,
            )
            await audit.start_run(agent_ctx, metadata={"label": "approve-start"})
            start = await svc.start(request=req, runtime=runtime, run_context=agent_ctx)
            key = start["idempotency_key"]
            assert start["status"] == ApprovalStatus.PENDING.value

            approver_ctx = run_context(
                tenant_id=tenant,
                user_id="approver-1",
                scopes=frozenset({"ticket:approve"}),
                ticket_id=ticket_id,
            )
            await audit.start_run(approver_ctx, metadata={"label": "approve"})
            approved = await svc.approve(
                request=req,
                approver_user_id="approver-1",
                runtime=runtime,
                run_context=approver_ctx,
            )
            assert approved.ok is True
            assert approved.status == ApprovalStatus.CONFIRMED
            assert approved.delivered is True
            assert approved.confirmed is True
            assert registry.get_status(key) == ApprovalStatus.CONFIRMED

            # 幂等：重复审批不重复执行
            again = await svc.approve(
                request=req,
                approver_user_id="approver-1",
                runtime=runtime,
                run_context=approver_ctx,
            )
            assert again.status == ApprovalStatus.CONFIRMED

            # 下发副作用只发生一次（MockVpnAdapter.reissue_config 幂等表）
            reissued = getattr(runtime.vpn_adapter, "_reissued", {})
            assert len(reissued.get(REISSUE_USER, {})) == 1, "重复审批不得重复下发"
            executed_count = await _count_agent_events(pool, tenant, "vpn_reissue_executed")
            assert executed_count == 1, "重复审批不得重复执行"

            # 显式执行 domain APPROVE 迁移（AWAITING_APPROVAL -> IN_PROGRESS，等价 API _domain_after_approve）
            await _domain_transition(tickets, tenant, ticket_id, TicketAction.APPROVE, "approver-1")
            after = await tickets.get(tenant, ticket_id)
            assert after.status == TicketStatus.IN_PROGRESS

            # workflow_operation 终态 committed
            op = await tickets.get_workflow_operation(
                tenant_id=tenant, ticket_id=ticket_id, operation_id=key
            )
            assert op is not None and op["status"] == "committed"
            return approved, after
        finally:
            await pool.close()

    approved, after = asyncio.run(run())
    assert after.status == TicketStatus.IN_PROGRESS


# ===========================================================================
# 4. VpnDiagnosisRepository 租户化 + 幂等（阶段二 P0#3）
# ===========================================================================


async def _create_ticket(tickets: TicketRepository, tenant_id: str, ticket_id: str) -> None:
    await tickets.create(
        tenant_id,
        CreateTicket(
            ticket_id=ticket_id,
            requester_id=REISSUE_USER,
            channel="web",
            title="VPN 无法连接",
            description="vpn_diagnosis",
            actor_type=ActorType.CUSTOMER,
            actor_id=REISSUE_USER,
        ),
    )


def _action(
    *, tenant_id: str, ticket_id: str, action_id: str, status: CustomerActionStatus
) -> VpnCustomerAction:
    return VpnCustomerAction(
        action_id=action_id,
        ticket_id=ticket_id,
        tenant_id=tenant_id,
        title="检查版本",
        instruction="打开客户端查看版本号",
        expected_result="版本号正确",
        risk_level="low",
        requires_agent=False,
        status=status,
    )


def test_get_action_is_tenant_and_ticket_scoped():
    """跨租户 / 跨工单 get_action 返回 None，同租户+同工单命中。"""

    async def run():
        pool, _audit, tickets, _assets = await _open_runtime()
        try:
            repo = VpnDiagnosisRepository(pool)
            tenant_a = f"tenant-{uuid4().hex}"
            tenant_b = f"tenant-{uuid4().hex}"
            ticket = f"ticket-{uuid4().hex[:10]}"
            action_id = f"act-{uuid4().hex[:12]}"
            await _create_ticket(tickets, tenant_a, ticket)

            await repo.add_action(
                _action(
                    tenant_id=tenant_a,
                    ticket_id=ticket,
                    action_id=action_id,
                    status=CustomerActionStatus.ISSUED,
                )
            )

            # 同租户 + 同工单 -> 命中
            found = await repo.get_action(tenant_a, ticket, action_id)
            assert found is not None
            assert found.action_id == action_id
            assert found.tenant_id == tenant_a
            assert found.ticket_id == ticket

            # 跨租户（同工单）-> None：不得读到他人动作
            assert await repo.get_action(tenant_b, ticket, action_id) is None

            # 跨工单（同租户）-> None
            assert await repo.get_action(tenant_a, "ticket-other", action_id) is None
        finally:
            await pool.close()

    asyncio.run(run())


def test_update_action_status_is_tenant_scoped_and_reports_miss():
    """同租户更新 rowcount=1；跨租户/跨工单 rowcount=0 返回 False 且不改他人。"""

    async def run():
        pool, _audit, tickets, _assets = await _open_runtime()
        try:
            repo = VpnDiagnosisRepository(pool)
            tenant_a = f"tenant-{uuid4().hex}"
            tenant_b = f"tenant-{uuid4().hex}"
            ticket = f"ticket-{uuid4().hex[:10]}"
            action_id = f"act-{uuid4().hex[:12]}"
            await _create_ticket(tickets, tenant_a, ticket)
            await repo.add_action(
                _action(
                    tenant_id=tenant_a,
                    ticket_id=ticket,
                    action_id=action_id,
                    status=CustomerActionStatus.ISSUED,
                )
            )

            # 同租户 + 同工单 -> 命中
            assert (
                await repo.update_action_status(
                    tenant_a, ticket, action_id, CustomerActionStatus.EXECUTED
                )
                is True
            )
            updated = await repo.get_action(tenant_a, ticket, action_id)
            assert updated is not None and updated.status == CustomerActionStatus.EXECUTED

            # 跨租户 -> rowcount=0 -> False，且不影响租户 a 的原动作
            assert (
                await repo.update_action_status(
                    tenant_b, ticket, action_id, CustomerActionStatus.CONFIRMED
                )
                is False
            )
            assert (
                await repo.get_action(tenant_a, ticket, action_id)
            ).status == CustomerActionStatus.EXECUTED

            # 跨工单 -> rowcount=0 -> False
            assert (
                await repo.update_action_status(
                    tenant_a, "ticket-other", action_id, CustomerActionStatus.CONFIRMED
                )
                is False
            )
        finally:
            await pool.close()

    asyncio.run(run())


def test_add_action_and_add_action_result_are_idempotent_upserts():
    """重复提交同一 action_id / result_id 不重复插入（ON CONFLICT 保留单行）。"""

    async def run():
        pool, _audit, tickets, _assets = await _open_runtime()
        try:
            repo = VpnDiagnosisRepository(pool)
            tenant = f"tenant-{uuid4().hex}"
            ticket = f"ticket-{uuid4().hex[:10]}"
            action_id = f"act-{uuid4().hex[:12]}"
            result_id = f"res-{uuid4().hex[:12]}"
            await _create_ticket(tickets, tenant, ticket)

            # 重复 add_action：同 action_id，第二次改状态，仍应只有一行
            await repo.add_action(
                _action(
                    tenant_id=tenant,
                    ticket_id=ticket,
                    action_id=action_id,
                    status=CustomerActionStatus.ISSUED,
                )
            )
            await repo.add_action(
                _action(
                    tenant_id=tenant,
                    ticket_id=ticket,
                    action_id=action_id,
                    status=CustomerActionStatus.EXECUTED,
                )
            )
            actions = await repo.list_actions(tenant, ticket)
            assert len(actions) == 1, "同 action_id 重复 upsert 不得产生新行"
            assert actions[0].status == CustomerActionStatus.EXECUTED

            # 重复 add_action_result：同 result_id 提交多次，仍应只有一行
            result = VpnCustomerActionResult(
                result_id=result_id,
                action_id=action_id,
                ticket_id=ticket,
                tenant_id=tenant,
                result="已重启，版本 3.4.2",
                submitted_by="customer-1",
            )
            await repo.add_action_result(result)
            await repo.add_action_result(result)
            results = await repo.list_action_results(tenant, ticket)
            assert len(results) == 1, "同 result_id 重复 upsert 不得产生新行"
            assert results[0].result == "已重启，版本 3.4.2"
        finally:
            await pool.close()

    asyncio.run(run())
