"""VPN 客户处置闭环 P0 边界测试（阶段二 P0#1/#3/#4 收口）。

背景：仓库依赖真实 PostgreSQL/Redis 的集成断言集中在 test_vpn_protection_postgres.py，
其 pytestmark 在无 TEST_DATABASE_URL/REDIS_URL 时整体跳过。本文件在**无外部数据库**的环境
下，用内存 fake repository（复刻 VpnDiagnosisRepository 的租户三元组语义）验证闭环**单一事实
来源 + 持久化失败 fail-loud + 跨租户/幂等/动作归属**的 P0 边界，确保代码改动在任何 CI 都能落地验证。

覆盖四类 P0 边界（验收项）：
    1. 动作归属：客户只能回填本租户+本工单下的动作；跨租户/跨工单/不存在 -> 拒绝。
    2. 幂等：同一动作已 EXECUTED 时重复提交被拒。
    3. 失败可见性（fail-loud）：repository 存在时，读取/落库抛错 -> VpnPersistenceError 冒泡，
       API 层映射为 503，而非 200 静默成功。
    4. 单一事实来源：repository 存在时 submit_action_result 从 repository.get_action 读取并校验，
       而不是回退到内存 registry。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.security import Principal, rate_limit_dependency
from backend.vpn.api_v2 import router as vpn_diagnosis_router
from backend.vpn.closed_loop import (
    VpnClosedLoopService,
    VpnCustomerActionError,
    VpnPersistenceError,
)
from backend.vpn.diagnosis import (
    CustomerActionStatus,
    DiagnosisRegistry,
    RiskLevel,
    VpnCustomerAction,
    VpnCustomerActionResult,
)
from src.my_agent.helpdesk import TicketAction, TicketStatus, transition_ticket

# ===========================================================================
# fakes / helpers
# ===========================================================================


class _FakeRepo:
    """内存版 VpnDiagnosisRepository：复刻租户三元组语义，并支持注入读写失败。

    只有「同租户 + 同工单 + 同 action_id」三元组能命中（跨租户/跨工单返回 None），
    与真实 PG 仓储 (tenant_id, ticket_id, action_id) WHERE 过滤一致。
    """

    def __init__(self) -> None:
        self.actions: dict[str, VpnCustomerAction] = {}
        self.results: dict[str, VpnCustomerActionResult] = {}
        self.runs: dict[str, Any] = {}
        self.escalations: dict[str, Any] = {}
        self._fail_get_action = False
        self._fail_add_result = False
        self._fail_update_status = False
        self._fail_save_run = False

    def seed_action(self, action: VpnCustomerAction) -> None:
        self.actions[action.action_id] = action

    # ---- 读 ----
    async def get_action(
        self, tenant_id: str, ticket_id: str, action_id: str
    ) -> VpnCustomerAction | None:
        if self._fail_get_action:
            raise RuntimeError("simulated db read failure")
        action = self.actions.get(action_id)
        if action is None or action.tenant_id != tenant_id or action.ticket_id != ticket_id:
            return None
        return action

    async def get_latest_run(self, tenant_id: str, ticket_id: str) -> Any:
        latest = None
        for run in self.runs.values():
            if run.tenant_id == tenant_id and run.ticket_id == ticket_id:
                if latest is None or run.created_at >= latest.created_at:
                    latest = run
        return latest

    async def list_runs(self, tenant_id: str, ticket_id: str) -> list[Any]:
        return [
            r for r in self.runs.values() if r.tenant_id == tenant_id and r.ticket_id == ticket_id
        ]

    async def list_actions(self, tenant_id: str, ticket_id: str) -> list[VpnCustomerAction]:
        return [
            a
            for a in self.actions.values()
            if a.tenant_id == tenant_id and a.ticket_id == ticket_id
        ]

    async def list_action_results(
        self, tenant_id: str, ticket_id: str
    ) -> list[VpnCustomerActionResult]:
        return [
            r
            for r in self.results.values()
            if r.tenant_id == tenant_id and r.ticket_id == ticket_id
        ]

    async def list_escalations(self, tenant_id: str, ticket_id: str) -> list[Any]:
        return [
            e
            for e in self.escalations.values()
            if e.tenant_id == tenant_id and e.ticket_id == ticket_id
        ]

    # ---- 写 ----
    async def save_run(self, run: Any) -> Any:
        if self._fail_save_run:
            raise RuntimeError("simulated db write failure")
        self.runs[run.run_id] = run
        return run

    async def add_action(self, action: VpnCustomerAction) -> VpnCustomerAction:
        self.actions[action.action_id] = action
        return action

    async def add_action_result(self, result: VpnCustomerActionResult) -> VpnCustomerActionResult:
        if self._fail_add_result:
            raise RuntimeError("simulated db write failure")
        self.results[result.result_id] = result
        return result

    async def update_action_status(
        self, tenant_id: str, ticket_id: str, action_id: str, status: CustomerActionStatus
    ) -> bool:
        if self._fail_update_status:
            raise RuntimeError("simulated db write failure")
        action = self.actions.get(action_id)
        if action is None or action.tenant_id != tenant_id or action.ticket_id != ticket_id:
            return False
        action.status = status
        return True

    async def add_escalation(self, escalation: Any) -> Any:
        self.escalations[escalation.escalation_id] = escalation
        return escalation


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def record_event(self, context, event_type, status=None, payload=None):
        self.events.append((event_type, status or "", payload or {}))


class _FakeTickets:
    def __init__(
        self, *, initial_status=TicketStatus.IN_PROGRESS, version=0, requester_id="user-042"
    ) -> None:
        self.cur_status = initial_status
        self.version = version
        self.requester_id = requester_id
        self.transition_calls: list[TicketAction] = []

    async def get(self, tenant_id, ticket_id):
        return SimpleNamespace(
            ticket_id=ticket_id,
            status=self.cur_status,
            version=self.version,
            requester_id=self.requester_id,
        )

    async def transition(self, tenant_id, command, scopes=None):
        self.transition_calls.append(command.action)
        self.cur_status = transition_ticket(self.cur_status, command, scopes=set(scopes or ()))
        return SimpleNamespace(
            ticket_id=command.ticket_id, status=self.cur_status, version=self.version
        )

    async def append_status_event(
        self, tenant_id, ticket_id, *, action, actor_type, actor_id, payload=None
    ):
        return True


class _StubDiagnosisService:
    """诊断服务桩（提供 provide_steps 结果），仅用于成功路径可能触发的再次诊断。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run_with_context(self, *, runtime, tenant_id, ticket_id, run_context):
        self.calls += 1
        return {
            "request": {
                "fault": "connection_failed",
                "ticket_id": ticket_id,
                "tenant_id": tenant_id,
            },
            "result": {
                "command": {
                    "command": "provide_steps",
                    "content": "1. 检查客户端版本",
                    "payload": {},
                    "reason_codes": ["fault_connection"],
                    "confidence": 0.91,
                },
                "must_handoff": False,
                "evaluation": {"must_handoff": False, "handoff_reasons": [], "confidence": 0.91},
                "tool_evidence": [
                    {"tool_name": "get_vpn_account_status", "content": "账号正常", "found": True}
                ],
            },
        }


def _action(
    *,
    tenant_id: str,
    ticket_id: str,
    action_id: str,
    status: CustomerActionStatus = CustomerActionStatus.ISSUED,
) -> VpnCustomerAction:
    return VpnCustomerAction(
        action_id=action_id,
        ticket_id=ticket_id,
        tenant_id=tenant_id,
        title="检查版本",
        instruction="打开客户端查看版本号",
        expected_result="版本号正确",
        risk_level=RiskLevel.LOW,
        requires_agent=False,
        status=status,
    )


def _make_svc(repo: _FakeRepo) -> VpnClosedLoopService:
    return VpnClosedLoopService(
        diagnosis_service=_StubDiagnosisService(),
        registry=DiagnosisRegistry(),
        repository=repo,
    )


def _make_runtime(svc, *, initial_status=TicketStatus.IN_PROGRESS, requester_id="user-042"):
    tickets = _FakeTickets(initial_status=initial_status, requester_id=requester_id)
    audit = _FakeAudit()
    runtime = SimpleNamespace(vpn_closed_loop=svc, tickets=tickets, audit=audit)
    return runtime, tickets, audit


def _rc(scope="ticket:customer", user_id="user-042", tenant_id="tenant-a"):
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id, scopes=frozenset({scope}))


# ===========================================================================
# 1. 动作归属：跨租户 / 跨工单 / 不存在 -> 拒绝
# ===========================================================================


def test_submit_action_via_repo_rejects_cross_tenant():
    repo = _FakeRepo()
    repo.seed_action(_action(tenant_id="tenant-a", ticket_id="t-1", action_id="act-1"))
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(svc)
    with pytest.raises(VpnCustomerActionError):
        asyncio.run(
            svc.submit_action_result(
                runtime=runtime,
                tenant_id="tenant-b",  # 跨租户访问 tenant-a 的动作
                ticket_id="t-1",
                action_id="act-1",
                result="x",
                run_context=_rc(user_id="user-999", tenant_id="tenant-b"),
            )
        )


def test_submit_action_via_repo_rejects_cross_ticket():
    repo = _FakeRepo()
    repo.seed_action(_action(tenant_id="tenant-a", ticket_id="t-1", action_id="act-1"))
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(svc)
    with pytest.raises(VpnCustomerActionError):
        asyncio.run(
            svc.submit_action_result(
                runtime=runtime,
                tenant_id="tenant-a",
                ticket_id="t-other",  # 跨工单
                action_id="act-1",
                result="x",
                run_context=_rc(user_id="user-042"),
            )
        )


def test_submit_action_via_repo_rejects_unknown_action():
    repo = _FakeRepo()
    svc = _make_svc(repo)  # 没有任何动作
    runtime, _tickets, _audit = _make_runtime(svc)
    with pytest.raises(VpnCustomerActionError):
        asyncio.run(
            svc.submit_action_result(
                runtime=runtime,
                tenant_id="tenant-a",
                ticket_id="t-1",
                action_id="act-missing",
                result="x",
                run_context=_rc(user_id="user-042"),
            )
        )


# ===========================================================================
# 2. 幂等：同一动作已 EXECUTED 时重复提交被拒
# ===========================================================================


def test_submit_action_via_repo_rejects_already_executed():
    repo = _FakeRepo()
    repo.seed_action(
        _action(
            tenant_id="tenant-a",
            ticket_id="t-1",
            action_id="act-1",
            status=CustomerActionStatus.EXECUTED,
        )
    )
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(svc)
    with pytest.raises(VpnCustomerActionError):
        asyncio.run(
            svc.submit_action_result(
                runtime=runtime,
                tenant_id="tenant-a",
                ticket_id="t-1",
                action_id="act-1",
                result="再次提交",
                run_context=_rc(user_id="user-042"),
            )
        )


# ===========================================================================
# 3. 失败可见性（fail-loud）：读取/落库抛错往上冒泡
# ===========================================================================


def test_submit_action_fail_loud_when_action_read_fails():
    repo = _FakeRepo()
    repo.seed_action(_action(tenant_id="tenant-a", ticket_id="t-1", action_id="act-1"))
    repo._fail_get_action = True  # DB 读失败
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(svc)
    with pytest.raises(VpnPersistenceError):
        asyncio.run(
            svc.submit_action_result(
                runtime=runtime,
                tenant_id="tenant-a",
                ticket_id="t-1",
                action_id="act-1",
                result="x",
                run_context=_rc(user_id="user-042"),
            )
        )


def test_submit_action_fail_loud_when_add_result_fails():
    repo = _FakeRepo()
    repo.seed_action(_action(tenant_id="tenant-a", ticket_id="t-1", action_id="act-1"))
    repo._fail_add_result = True  # 客户结果落库失败
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(svc)
    with pytest.raises(VpnPersistenceError):
        asyncio.run(
            svc.submit_action_result(
                runtime=runtime,
                tenant_id="tenant-a",
                ticket_id="t-1",
                action_id="act-1",
                result="已处理",
                run_context=_rc(user_id="user-042"),
            )
        )


def test_submit_action_fail_loud_when_update_status_fails():
    repo = _FakeRepo()
    repo.seed_action(_action(tenant_id="tenant-a", ticket_id="t-1", action_id="act-1"))
    repo._fail_update_status = True  # 动作状态落库失败
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(svc)
    with pytest.raises(VpnPersistenceError):
        asyncio.run(
            svc.submit_action_result(
                runtime=runtime,
                tenant_id="tenant-a",
                ticket_id="t-1",
                action_id="act-1",
                result="已处理",
                run_context=_rc(user_id="user-042"),
            )
        )


# ===========================================================================
# 4. 单一事实来源：repository 存在时从 repository 读取（而非内存 registry）
# ===========================================================================


def test_submit_action_via_repo_reads_action_from_repo_not_registry():
    repo = _FakeRepo()
    repo.seed_action(_action(tenant_id="tenant-a", ticket_id="t-1", action_id="act-1"))
    svc = _make_svc(repo)  # registry 为空；若回退到内存 registry 会因找不到动作而报错
    runtime, _tickets, _audit = _make_runtime(svc)
    out = asyncio.run(
        svc.submit_action_result(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            action_id="act-1",
            result="已处理",
            run_context=_rc(user_id="user-042"),
        )
    )
    assert out["action_result"]["action_id"] == "act-1"
    # 动作已被推进到 EXECUTED（写回 repository，单一事实来源）。
    updated = asyncio.run(repo.get_action("tenant-a", "t-1", "act-1"))
    assert updated is not None
    assert updated.status == CustomerActionStatus.EXECUTED


# ===========================================================================
# 5. HTTP API 错误映射（fail-loud -> 503；跨租户/幂等 -> 409）
# ===========================================================================


def _make_app(runtime):
    app = FastAPI()
    app.include_router(vpn_diagnosis_router)
    app.state.runtime = runtime
    return app


def _install_principal(app: FastAPI, principal: Principal):
    def _dep():
        return principal

    app.dependency_overrides[rate_limit_dependency] = _dep


def _principal(user_id, scopes, tenant_id="tenant-a") -> Principal:
    return Principal(tenant_id=tenant_id, user_id=user_id, scopes=frozenset(scopes))


def test_api_persist_failure_returns_503():
    """repository 落库失败 -> API 返回 503（失败不静默）。"""
    repo = _FakeRepo()
    repo.seed_action(_action(tenant_id="tenant-a", ticket_id="t-1", action_id="act-1"))
    repo._fail_add_result = True
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(
        svc, initial_status=TicketStatus.AWAITING_CUSTOMER_ACTION
    )
    runtime.vpn_closed_loop = svc
    app = _make_app(runtime)
    _install_principal(app, _principal("user-042", ("ticket:customer",), tenant_id="tenant-a"))
    with TestClient(app) as client:
        resp = client.post("/tickets/t-1/vpn/actions/act-1/result", json={"result": "已处理"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "VPN 数据持久化失败"


def test_api_cross_tenant_action_returns_409():
    """跨租户动作（动作属于其它租户）-> 409 拒绝。"""
    repo = _FakeRepo()
    repo.seed_action(_action(tenant_id="tenant-b", ticket_id="t-1", action_id="act-1"))
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(
        svc, initial_status=TicketStatus.AWAITING_CUSTOMER_ACTION
    )
    runtime.vpn_closed_loop = svc
    app = _make_app(runtime)
    _install_principal(app, _principal("user-042", ("ticket:customer",), tenant_id="tenant-a"))
    with TestClient(app) as client:
        resp = client.post("/tickets/t-1/vpn/actions/act-1/result", json={"result": "已处理"})
    assert resp.status_code == 409, resp.text


def test_api_already_executed_action_returns_409():
    """已 EXECUTED 动作重复提交 -> 409（幂等拒绝）。"""
    repo = _FakeRepo()
    repo.seed_action(
        _action(
            tenant_id="tenant-a",
            ticket_id="t-1",
            action_id="act-1",
            status=CustomerActionStatus.EXECUTED,
        )
    )
    svc = _make_svc(repo)
    runtime, _tickets, _audit = _make_runtime(
        svc, initial_status=TicketStatus.AWAITING_CUSTOMER_ACTION
    )
    runtime.vpn_closed_loop = svc
    app = _make_app(runtime)
    _install_principal(app, _principal("user-042", ("ticket:customer",), tenant_id="tenant-a"))
    with TestClient(app) as client:
        resp = client.post("/tickets/t-1/vpn/actions/act-1/result", json={"result": "已处理"})
    assert resp.status_code == 409, resp.text


# ===========================================================================
# 6. repository=None 回退：不抛 persist（保留单测内存版可跑口径）
# ===========================================================================


def test_submit_action_no_repo_falls_back_without_persist_error():
    """repository=None（单测/无库）时走内存 registry，_persist_* 直接返回，不抛 VpnPersistenceError。

    这是 t3 约定的关键边界：fail-loud 只在 repository 存在（有库/生产）时触发，
    单测内存版必须能继续跑，不因新增 VpnPersistenceError 而破坏。
    """
    registry = DiagnosisRegistry()
    registry.add_action(_action(tenant_id="tenant-a", ticket_id="t-1", action_id="act-1"))
    svc = VpnClosedLoopService(
        diagnosis_service=_StubDiagnosisService(),
        registry=registry,
        repository=None,  # 显式无库：保留内存版可测口径
    )
    runtime, _tickets, _audit = _make_runtime(svc, initial_status=TicketStatus.IN_PROGRESS)
    # 若 _persist_* 错误地抛 VpnPersistenceError，此处会失败；成功证明 repository=None 时不 fail-loud。
    out = asyncio.run(
        svc.submit_action_result(
            runtime=runtime,
            tenant_id="tenant-a",
            ticket_id="t-1",
            action_id="act-1",
            result="已处理",
            run_context=_rc(user_id="user-042"),
        )
    )
    assert out["action_result"]["action_id"] == "act-1"
    assert out["transition"] is False  # IN_PROGRESS 下 PROVIDE_ACTION_RESULT 不迁移，跳过再次诊断
    # 内存 registry 已推进动作状态为 EXECUTED。
    action = registry.get_action("act-1")
    assert action is not None
    assert action.status == CustomerActionStatus.EXECUTED
