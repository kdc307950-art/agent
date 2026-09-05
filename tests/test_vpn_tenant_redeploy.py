"""租户级 VPN 配置重推的 preview、审批复核与执行门禁。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from backend.run_context import RunContext
from backend.vpn.approval import (
    ApprovalStatus,
    ReissueRegistry,
    build_reissue_request_from_payload,
)
from backend.vpn.reissue_service import VpnReissueService
from src.my_agent.helpdesk import TicketStatus, transition_ticket


class _Audit:
    def __init__(self):
        self.events = []

    async def record_event(self, context, event_type, status=None, payload=None):
        self.events.append((event_type, status, payload or {}))


class _Tickets:
    def __init__(self):
        self.status = TicketStatus.IN_PROGRESS
        self.version = 0
        self.transition_calls = []
        self.workflow_status = {}

    async def get(self, tenant_id, ticket_id):
        return SimpleNamespace(status=self.status, version=self.version, ticket_id=ticket_id)

    async def transition(self, tenant_id, command, scopes=None):
        self.transition_calls.append(command.action)
        self.status = transition_ticket(self.status, command, scopes=set(scopes or ()))
        self.version += 1
        return SimpleNamespace(status=self.status, version=self.version, ticket_id=command.ticket_id)

    async def start_workflow_operation(self, **kwargs):
        self.workflow_status[kwargs["operation_id"]] = "started"

    async def mark_workflow_operation_committed(self, **kwargs):
        self.workflow_status[kwargs["operation_id"]] = "committed"


class _TenantGateway:
    def __init__(self, hashes, *, status="previewed", redeploy_result=None):
        self.hashes = list(hashes)
        self.status = status
        self.redeploy_result = redeploy_result or {
            "status": "confirmed",
            "delivered": True,
            "confirmed": True,
            "vendor_task_id": 101,
        }
        self.preview_calls = 0
        self.redeploy_calls = []

    async def validate_tenant_target(self, *, tenant_id):
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "target": {"adom": "ADOM_A", "device": "FGT_A", "vdom": "root", "package": "PKG_A"},
        }

    async def preview_tenant_config(self, *, tenant_id):
        index = min(self.preview_calls, len(self.hashes) - 1)
        self.preview_calls += 1
        diff_hash = self.hashes[index]
        return {
            "ok": True,
            "status": self.status,
            "tenant_id": tenant_id,
            "target": {"adom": "ADOM_A", "device": "FGT_A", "vdom": "root", "package": "PKG_A"},
            "diff_snapshot": {"changes": ["policy-1"]} if self.status != "noop" else {},
            "diff_hash": diff_hash,
        }

    async def redeploy_tenant_vpn_config(self, **kwargs):
        self.redeploy_calls.append(kwargs)
        return dict(self.redeploy_result)


def _ctx(*, scopes=frozenset({"ticket:agent", "ticket:approve"})):
    return RunContext(
        run_id="tenant-redeploy-test",
        request_id="req-tenant-redeploy",
        tenant_id="tenant-a",
        user_id="operator-1",
        thread_id="vpn:tenant-a:t-1",
        scopes=scopes,
        deadline=time.time() + 60,
        allowed_tools=None,
    )


def _request():
    return build_reissue_request_from_payload(
        {"action": "redeploy_tenant_vpn_config", "ticket_id": "t-1"},
        tenant_id="tenant-a",
    )


def _runtime(gateway):
    return SimpleNamespace(
        audit=_Audit(),
        tickets=_Tickets(),
        vpn_command_gateway=gateway,
    )


def test_tenant_payload_cannot_mix_single_user_target():
    try:
        build_reissue_request_from_payload(
            {
                "action": "redeploy_tenant_vpn_config",
                "ticket_id": "t-1",
                "user_id": "user-1",
            },
            tenant_id="tenant-a",
        )
    except ValueError as exc:
        assert "user_id" in str(exc)
    else:
        raise AssertionError("租户级 action 不应接受单用户目标")


def test_tenant_preview_noop_does_not_create_approval_or_install():
    gateway = _TenantGateway(["empty-hash"], status="noop")
    runtime = _runtime(gateway)
    service = VpnReissueService(registry=ReissueRegistry())
    result = asyncio.run(service.start(request=_request(), runtime=runtime, run_context=_ctx()))

    assert result["status"] == "noop"
    assert result["approval_required"] is False
    assert asyncio.run(
        service.store.get_operation(tenant_id="tenant-a", operation_id="redeploy:tenant-a:t-1")
    ) is None
    assert gateway.redeploy_calls == []


def test_tenant_approval_rejects_changed_preview_without_install():
    gateway = _TenantGateway(["hash-v1", "hash-v2"])
    runtime = _runtime(gateway)
    service = VpnReissueService(registry=ReissueRegistry())
    request = _request()

    started = asyncio.run(service.start(request=request, runtime=runtime, run_context=_ctx()))
    assert started["status"] == ApprovalStatus.PENDING.value
    result = asyncio.run(
        service.approve(
            operation_id=request.idempotency_key,
            approver_user_id="approver-1",
            runtime=runtime,
            run_context=_ctx(),
        )
    )

    assert result.ok is False
    assert result.error_code == "precheck_failed"
    assert "preview_changed" in result.detail["fail_reasons"]
    assert runtime.tickets.status == TicketStatus.AWAITING_APPROVAL
    assert gateway.redeploy_calls == []


def test_tenant_approval_uses_approved_hash_for_single_install():
    gateway = _TenantGateway(["hash-v1"])
    runtime = _runtime(gateway)
    service = VpnReissueService(registry=ReissueRegistry())
    request = _request()

    asyncio.run(service.start(request=request, runtime=runtime, run_context=_ctx()))
    result = asyncio.run(
        service.approve(
            operation_id=request.idempotency_key,
            approver_user_id="approver-1",
            runtime=runtime,
            run_context=_ctx(),
        )
    )

    assert result.ok is True
    assert result.status == ApprovalStatus.CONFIRMED
    assert len(gateway.redeploy_calls) == 1
    assert gateway.redeploy_calls[0]["approved_diff_hash"] == "hash-v1"
    assert gateway.redeploy_calls[0]["action"] == "redeploy_tenant_vpn_config"


def test_tenant_completed_operation_is_idempotent_on_repeated_start():
    gateway = _TenantGateway(["hash-v1"])
    runtime = _runtime(gateway)
    service = VpnReissueService(registry=ReissueRegistry())
    request = _request()

    asyncio.run(service.start(request=request, runtime=runtime, run_context=_ctx()))
    asyncio.run(
        service.approve(
            operation_id=request.idempotency_key,
            approver_user_id="approver-1",
            runtime=runtime,
            run_context=_ctx(),
        )
    )
    repeated = asyncio.run(service.start(request=request, runtime=runtime, run_context=_ctx()))

    assert repeated["status"] == ApprovalStatus.CONFIRMED.value
    assert repeated["existing_result"]["action"] == "redeploy_tenant_vpn_config"
    assert gateway.redeploy_calls == [
        {
            "action": "redeploy_tenant_vpn_config",
            "approved_diff_hash": "hash-v1",
            "enforce_preview": True,
            "idempotency_key": "redeploy:tenant-a:t-1",
            "tenant_id": "tenant-a",
            "target_version": "tenant-current",
            "user_id": None,
        }
    ]


def test_submission_unknown_is_reconcilable_but_not_a_retryable_failure():
    gateway = _TenantGateway(
        ["hash-v1"],
        redeploy_result={
            "status": "failed",
            "error_code": "submission_unknown",
            "delivered": False,
            "confirmed": False,
        },
    )
    runtime = _runtime(gateway)
    service = VpnReissueService(registry=ReissueRegistry())
    request = _request()

    asyncio.run(service.start(request=request, runtime=runtime, run_context=_ctx()))
    result = asyncio.run(
        service.approve(
            operation_id=request.idempotency_key,
            approver_user_id="approver-1",
            runtime=runtime,
            run_context=_ctx(),
        )
    )

    assert result.status == ApprovalStatus.EXECUTION_UNKNOWN
    assert result.error_code == "submission_unknown"
    op = asyncio.run(
        service.store.get_operation(
            tenant_id="tenant-a", operation_id=request.idempotency_key
        )
    )
    assert op.status == ApprovalStatus.EXECUTION_UNKNOWN
    assert len(gateway.redeploy_calls) == 1
