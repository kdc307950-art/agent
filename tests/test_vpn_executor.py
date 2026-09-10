"""VPN Diagnosis Agent 业务层校验/执行单元测试（backend/vpn/executor.py）。

覆盖契约 §8 + 验收「agent 无法直接改工单、无法执行副作用」：
    - execute_diagnosis_command 对 5 个允许命令做命令->状态机映射并执行；
    - 7 个禁止命令一律拒绝（DiagnosisCommandRejected）且零副作用；
    - provide_steps 仅产出草稿、不直接发消息（不触发状态机迁移）；
    - escalate_incident 进入人工队列（team-service-desk）且不自动回复。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from backend.run_context import RunContext
from backend.vpn import (
    DiagnosisCommand,
    DiagnosisCommandRejected,
    DiagnosisCommandType,
    DiagnosisCommandValidator,
    execute_diagnosis_command,
)
from backend.vpn.executor import HUMAN_QUEUE_TEAM_ID
from src.my_agent.helpdesk import TicketAction


class _FakeAudit:
    def __init__(self):
        self.events = []

    async def record_event(self, context, event_type, **kwargs):
        self.events.append((context.run_id, event_type, kwargs))


class _FakeRuntime:
    """带 audit 与 tickets.transition 假实现的运行时（不触真实 DB）。"""

    def __init__(self):
        self.audit = _FakeAudit()
        self.transition_calls: list[dict[str, Any]] = []
        self.tickets = SimpleNamespace(transition=self._transition)

    async def _transition(self, tenant_id, ticket_cmd, scopes=None):
        self.transition_calls.append(
            {"tenant_id": tenant_id, "ticket_cmd": ticket_cmd, "scopes": scopes}
        )


def _run_context(tenant_id="tenant-a", user_id="user-1", scopes=frozenset({"ticket:agent"})):
    return RunContext(
        run_id="run-vpn-exec",
        request_id="req-1",
        tenant_id=tenant_id,
        user_id=user_id,
        thread_id=f"vpn:{tenant_id}:t-1",
        scopes=scopes,
        deadline=time.time() + 60,
        allowed_tools=None,
    )


def _cmd(command: DiagnosisCommandType, payload=None, content="命令文本", confidence=0.9):
    return DiagnosisCommand(
        command=command,
        content=content,
        payload=payload or {},
        reason_codes=[],
        confidence=confidence,
    )


async def _execute(command, runtime, rc, ticket_id="t-1"):
    return await execute_diagnosis_command(
        command, runtime=runtime, run_context=rc, ticket_id=ticket_id
    )


# ========== 5 个允许命令：校验 + 映射 + 执行 ==========


@pytest.mark.parametrize(
    ("command_type", "expected_action"),
    [
        (DiagnosisCommandType.ASK_CUSTOMER, TicketAction.REQUEST_INFORMATION),
        (DiagnosisCommandType.PROVIDE_STEPS, TicketAction.PROPOSE_ANSWER),
        (DiagnosisCommandType.ASSIGN_AGENT, TicketAction.ASSIGN),
        (DiagnosisCommandType.ESCALATE_INCIDENT, TicketAction.QUEUE),
        (DiagnosisCommandType.REQUEST_APPROVAL, TicketAction.REQUEST_APPROVAL),
    ],
)
def test_all_five_allowed_commands_map_to_ticket_action(command_type, expected_action):
    validator = DiagnosisCommandValidator()
    cmd = _cmd(command_type)
    # 集合校验通过与映射正确
    validator.validate(cmd)
    assert validator.to_ticket_action(cmd) == expected_action


@pytest.mark.parametrize(
    ("command_type", "expected_action"),
    [
        (DiagnosisCommandType.ASK_CUSTOMER, TicketAction.REQUEST_INFORMATION),
        (DiagnosisCommandType.ASSIGN_AGENT, TicketAction.ASSIGN),
        (DiagnosisCommandType.REQUEST_APPROVAL, TicketAction.REQUEST_APPROVAL),
        (DiagnosisCommandType.ESCALATE_INCIDENT, TicketAction.QUEUE),
    ],
)
def test_execute_non_draft_command_transitions(command_type, expected_action):
    runtime = _FakeRuntime()
    rc = _run_context()
    cmd = _cmd(command_type, {"ticket_id": "t-1"})
    result = asyncio.run(_execute(cmd, runtime, rc))
    assert result.ok is True
    assert result.action == expected_action.value
    assert result.ticket_id == "t-1"
    assert len(runtime.transition_calls) == 1
    ticket_cmd = runtime.transition_calls[0]["ticket_cmd"]
    assert ticket_cmd.action == expected_action
    assert ticket_cmd.actor_type.value == "agent"


# ========== provide_steps：仅草稿，不直接发消息 ==========


def test_execute_provide_steps_is_draft_only_no_transition():
    runtime = _FakeRuntime()
    rc = _run_context()
    cmd = _cmd(
        DiagnosisCommandType.PROVIDE_STEPS, {"ticket_id": "t-1"}, content="排查步骤：检查客户端版本"
    )
    result = asyncio.run(_execute(cmd, runtime, rc))
    assert result.ok is True
    assert result.reason == "draft_only"
    assert result.draft == "排查步骤：检查客户端版本"
    # 草稿-only：不触发状态机迁移（零副作用，不直接发消息）
    assert runtime.transition_calls == []


# ========== escalate_incident：进人工队列，不自动回复 ==========


def test_execute_escalate_incident_routes_to_human_queue():
    runtime = _FakeRuntime()
    rc = _run_context()
    cmd = _cmd(DiagnosisCommandType.ESCALATE_INCIDENT, {"ticket_id": "t-1"})
    result = asyncio.run(_execute(cmd, runtime, rc))
    assert result.ok is True
    assert result.action == TicketAction.QUEUE.value
    assert "人工队列" in (result.detail or "")
    assert HUMAN_QUEUE_TEAM_ID in (result.detail or "")
    ticket_cmd = runtime.transition_calls[0]["ticket_cmd"]
    assert ticket_cmd.action == TicketAction.QUEUE
    assert ticket_cmd.payload.get("team_id") == HUMAN_QUEUE_TEAM_ID


# ========== 禁止命令拒绝（零副作用） ==========


class _DuckCmd:
    """鸭子类型命令：允许我们测试 validator 对禁止/未注册命令值的拒绝分支。

    DiagnosisCommand.command 被枚举约束为 5 个允许值，无法构造禁止命令对象；
    这里用鸭子类型对象直测业务校验器的防御分支（forbidden / not_in_allowed_set）。
    """

    def __init__(self, command: str):
        self.command = command


@pytest.mark.parametrize(
    "forbidden",
    [
        "reset_password",
        "unlock_account",
        "grant_vpn_permission",
        "modify_vpn_config",
        "restart_gateway",
        "close_ticket",
        "send_customer_message",
    ],
)
def test_validator_rejects_each_forbidden_command(forbidden):
    validator = DiagnosisCommandValidator()
    with pytest.raises(DiagnosisCommandRejected) as exc:
        validator.validate(_DuckCmd(forbidden))
    assert exc.value.reason == "forbidden_command"


def test_validator_rejects_not_in_allowed_set():
    validator = DiagnosisCommandValidator()
    with pytest.raises(DiagnosisCommandRejected) as exc:
        validator.validate(_DuckCmd("not_a_real_command"))
    assert exc.value.reason == "not_in_allowed_set"


@pytest.mark.parametrize(
    "forbidden",
    [
        "reset_password",
        "unlock_account",
        "grant_vpn_permission",
        "modify_vpn_config",
        "restart_gateway",
        "close_ticket",
        "send_customer_message",
    ],
)
def test_execute_rejects_forbidden_command_zero_side_effect(forbidden):
    """禁止命令经 executor 拒绝，且不触发任何状态机迁移（零副作用）。"""
    runtime = _FakeRuntime()
    rc = _run_context()
    with pytest.raises(DiagnosisCommandRejected):
        asyncio.run(
            execute_diagnosis_command(
                _DuckCmd(forbidden), runtime=runtime, run_context=rc, ticket_id="t-1"
            )
        )
    assert runtime.transition_calls == []


# ========== 其它拒绝路径 ==========


def test_execute_rejects_missing_ticket_id():
    """命令 payload 缺 ticket_id 且未显式提供 -> 拒绝。"""
    runtime = _FakeRuntime()
    rc = _run_context()
    cmd = _cmd(DiagnosisCommandType.ASK_CUSTOMER, {})
    with pytest.raises(DiagnosisCommandRejected) as exc:
        asyncio.run(execute_diagnosis_command(cmd, runtime=runtime, run_context=rc))
    assert exc.value.reason == "missing_ticket_id"
    assert runtime.transition_calls == []
