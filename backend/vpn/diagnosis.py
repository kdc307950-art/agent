"""LANGGraph VPN 客户处置闭环 —— 持久化领域对象与登记表（阶段二）。

模块归属：backend/vpn。设计目标（对齐 vpn-v1-scope / vpn-diagnosis-agent-contract）：
    - 在「只读诊断 Agent（产出 DiagnosisCommand）」之上，补齐「客户处置闭环」的
      结构化落库：一次诊断运行、证据条目、给客户的排查步骤、客户回填结果、升级记录。
    - 所有模型 ``extra="forbid"``：拒绝模型/调用方自由注入字段，与 DiagnosisCommand /
      ReissueActionRequest 等既有契约一致。
    - 不直接依赖 runtime：本模块只定义纯数据契约与内存登记表（DiagnosisRegistry），
      服务编排（backend/vpn/closed_loop.py）在运行时显式接收 runtime / run_context。
    - 持久化策略：默认用内存登记表（与 reissue 的 ReissueRegistry 对齐，可单测）；
      生产可把登记表映射到 schema.py 新增的 vpn_* 表（见 backend/schema.py v23）。

红线：本模块不产生任何业务副作用（不发消息、不改工单状态）；状态机迁移由
closed_loop.py 经 domain.transition_ticket / tickets.transition 受控执行。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.my_agent.helpdesk import RiskLevel


def UTC_NOW() -> datetime:
    """返回当前 UTC 时间；作为 pydantic ``Field(default_factory=...)`` 的回调。"""
    return datetime.now(UTC)

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:-]+$"


# ===========================================================================
# 枚举
# ===========================================================================


class DiagnosisRunStatus(StrEnum):
    """一次 VPN 诊断运行的终身状态。"""

    DIAGNOSING = "diagnosing"          # 正在诊断 / 已产出命令等待处置
    COMPLETED = "completed"            # 已落定（解决/关闭）
    HANDED_OFF = "handed_off"          # 已升级人工接管
    FAILED = "failed"                  # 诊断失败（无可用命令）
    CANCELLED = "cancelled"            # 取消


class CustomerActionStatus(StrEnum):
    """给客户的排查步骤的可用状态。"""

    ISSUED = "issued"                  # 已产生，待客户执行
    EXECUTED = "executed"              # 客户已回填结果
    CONFIRMED = "confirmed"            # 结果确认有效
    ABANDONED = "abandoned"            # 客户放弃/超时
    SUPERSEDED = "superseded"          # 被后续排查步骤取代


class EscalationStatus(StrEnum):
    """VPN 升级记录状态。"""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    CLOSED = "closed"


# ===========================================================================
# 领域对象（全部 extra="forbid"）
# ===========================================================================


class VpnDiagnosisFinding(BaseModel):
    """一次诊断里的一条证据项（来自只读工具返回的依据）。"""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(default="", max_length=64, pattern=_IDENTIFIER_PATTERN)
    evidence: str = Field(default="", max_length=4_000)
    document_id: str | None = Field(default=None, max_length=128)
    document_version: str | None = Field(default=None, max_length=64)
    chunk_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=256)
    found: bool = True


class VpnDiagnosisRun(BaseModel):
    """一次 VPN 诊断运行（诊断过程与结论的结构化快照）。

    字段：
        run_id / ticket_id / tenant_id : 标识（租户隔离由 run_context 提供）
        fault                        : vpn_fault（对齐 VPN_FAULT_*）
        hypothesis                   : 诊断假设（结论）
        confidence                   : 0..1
        evidence                     : VpnDiagnosisFinding 列表
        ruled_out                    : 已排除的假设（reason_codes）
        next_action                  : 落定的处置命令（DiagnosisCommandType 值）
        reason_codes                 : 决策依据编码（审计用）
        status                       : DiagnosisRunStatus
        created_at / updated_at      : 时间戳
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    tenant_id: str = Field(min_length=1, max_length=128)
    fault: str = Field(default="connection_failed", max_length=64)
    hypothesis: str = Field(default="", max_length=4_000)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[VpnDiagnosisFinding] = Field(default_factory=list, max_length=64)
    ruled_out: list[str] = Field(default_factory=list, max_length=64)
    next_action: str | None = Field(default=None, max_length=64)
    reason_codes: list[str] = Field(default_factory=list, max_length=64)
    status: DiagnosisRunStatus = DiagnosisRunStatus.DIAGNOSING
    created_at: datetime = Field(default_factory=UTC_NOW)
    updated_at: datetime = Field(default_factory=UTC_NOW)


class VpnCustomerAction(BaseModel):
    """给客户的一条排查步骤（provide_steps 草稿的结构化落库）。

    必填字段（对齐需求）：action_id / title / instruction / expected_result /
    risk_level / requires_agent。ticket_id / tenant_id 用于租户隔离与归属校验。
    """

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128, pattern=_IDENTIFIER_PATTERN)
    title: str = Field(min_length=1, max_length=256)
    instruction: str = Field(min_length=1, max_length=8_000)
    expected_result: str = Field(min_length=1, max_length=1_024)
    risk_level: RiskLevel = Field(default=RiskLevel.LOW)
    requires_agent: bool = Field(default=False)
    status: CustomerActionStatus = CustomerActionStatus.ISSUED
    order: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=UTC_NOW)


class VpnCustomerActionResult(BaseModel):
    """客户对某条排查步骤的执行结果回填。"""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128, pattern=_IDENTIFIER_PATTERN)
    result: str = Field(min_length=1, max_length=16_000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    details: str = Field(default="", max_length=4_000)
    submitted_by: str = Field(default="", max_length=128, pattern=_IDENTIFIER_PATTERN)
    submitted_at: datetime = Field(default_factory=UTC_NOW)


class VpnEscalation(BaseModel):
    """一次 VPN 升级记录（多用户影响 / 身份缺失 / 证据不足 / 低置信度等转人工）。"""

    model_config = ConfigDict(extra="forbid")

    escalation_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128, pattern=_IDENTIFIER_PATTERN)
    reason: str = Field(default="", max_length=2_048)
    reason_codes: list[str] = Field(default_factory=list, max_length=64)
    target_queue: str = Field(default="team-service-desk", max_length=128)
    status: EscalationStatus = EscalationStatus.OPEN
    created_at: datetime = Field(default_factory=UTC_NOW)


# ===========================================================================
# 内存登记表（生产可映射到 schema.py 的 vpn_* 表）
# ===========================================================================


@dataclass
class DiagnosisRegistry:
    """VPN 客户处置闭环领域对象的登记表（内存实现，可单测）。

    生产环境应把该表映射到 backend/schema.py v23 新增的 vpn_diagnosis_runs /
    vpn_customer_actions / vpn_customer_action_results / vpn_escalations 表；
    此处提供支持单元测试的内存版，保证「诊断运行 / 排查步骤 / 客户结果 / 升级」
    在纯领域层可验证，且 API / 服务共用同一实例消除双入口不一致。
    """

    _runs: dict[str, VpnDiagnosisRun] = field(default_factory=dict)
    _ticket_runs: dict[str, list[str]] = field(default_factory=dict)
    _actions: dict[str, list[VpnCustomerAction]] = field(default_factory=dict)
    _action_results: dict[str, list[VpnCustomerActionResult]] = field(default_factory=dict)
    _action_by_id: dict[str, VpnCustomerAction] = field(default_factory=dict)
    _escalations: dict[str, list[VpnEscalation]] = field(default_factory=dict)

    # ---- 诊断运行 ----

    def save_run(self, run: VpnDiagnosisRun) -> VpnDiagnosisRun:
        self._runs[run.run_id] = run
        self._ticket_runs.setdefault(run.ticket_id, []).append(run.run_id)
        return run

    def get_run(self, run_id: str) -> VpnDiagnosisRun | None:
        return self._runs.get(run_id)

    def list_runs(self, ticket_id: str) -> list[VpnDiagnosisRun]:
        return [self._runs[rid] for rid in self._ticket_runs.get(ticket_id, []) if rid in self._runs]

    def get_latest_run(self, ticket_id: str) -> VpnDiagnosisRun | None:
        runs = self.list_runs(ticket_id)
        return runs[-1] if runs else None

    # ---- 排查步骤 ----

    def add_action(self, action: VpnCustomerAction) -> VpnCustomerAction:
        self._actions.setdefault(action.ticket_id, []).append(action)
        self._action_by_id[action.action_id] = action
        return action

    def list_actions(self, ticket_id: str) -> list[VpnCustomerAction]:
        return list(self._actions.get(ticket_id, []))

    def get_action(self, action_id: str) -> VpnCustomerAction | None:
        return self._action_by_id.get(action_id)

    # ---- 客户结果 ----

    def add_action_result(self, result: VpnCustomerActionResult) -> VpnCustomerActionResult:
        self._action_results.setdefault(result.action_id, []).append(result)
        # 回填该步骤状态为 EXECUTED
        action = self._action_by_id.get(result.action_id)
        if action is not None:
            action.status = CustomerActionStatus.EXECUTED
        return result

    def list_action_results(self, action_id: str) -> list[VpnCustomerActionResult]:
        return list(self._action_results.get(action_id, []))

    def list_ticket_results(self, ticket_id: str) -> list[VpnCustomerActionResult]:
        results: list[VpnCustomerActionResult] = []
        for action in self.list_actions(ticket_id):
            results.extend(self.list_action_results(action.action_id))
        return results

    # ---- 升级记录 ----

    def add_escalation(self, escalation: VpnEscalation) -> VpnEscalation:
        self._escalations.setdefault(escalation.ticket_id, []).append(escalation)
        return escalation

    def list_escalations(self, ticket_id: str) -> list[VpnEscalation]:
        return list(self._escalations.get(ticket_id, []))


# ===========================================================================
# provide_steps 草稿 -> VpnCustomerAction 列表（结构化落库）
# ===========================================================================


def _split_steps(text: str) -> list[str]:
    """把排查步骤草稿切成若干步骤文本。

    规则：优先按「行号 + 点号」（1. / 1）或「编号括号」（(1)）切分；否则按换行切分；
    只剩一行时按原样返回单条。空/纯空白返回空列表。
    """
    if not text or not text.strip():
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    numbered: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(?:\(?(\d+)\)?[\.、)]?\s*)(.+)$", line)
        if match and int(match.group(1)) == index + 1:
            numbered.append((index, match.group(2)))
        else:
            return lines  # 不是整齐编号列表，退回整行切分
    # 编号列表完整：以第 1 条起始序号归一（允许 0/1 开头）
    if numbered:
        return [content for _index, content in numbered]
    return lines


def parse_steps_to_actions(
    draft: str,
    *,
    ticket_id: str,
    tenant_id: str,
    run_id: str | None = None,
    risk_level: RiskLevel = RiskLevel.LOW,
    requires_agent: bool = False,
) -> list[VpnCustomerAction]:
    """把 provide_steps 的草稿文本解析为 VpnCustomerAction 列表。

    无法切分时回退为单条动作（title=「排查步骤」，instruction=草稿全文）。
    """
    steps = _split_steps(draft)
    if not steps:
        steps = [draft.strip()] if draft and draft.strip() else []
    actions: list[VpnCustomerAction] = []
    for order, step in enumerate(steps):
        title = _action_title(step, order)
        actions.append(
            VpnCustomerAction(
                action_id=_action_id(ticket_id, run_id, order),
                ticket_id=ticket_id,
                tenant_id=tenant_id,
                run_id=run_id,
                title=title,
                instruction=step,
                expected_result=_expected_result(step),
                risk_level=risk_level,
                requires_agent=requires_agent,
                order=order,
            )
        )
    return actions


def _action_title(step: str, order: int) -> str:
    first = step.splitlines()[0].strip() if step and step.strip() else ""
    title = first[:64] if first else "排查步骤"
    if len(first) > 64:
        title += "…"
    return title or f"排查步骤 {order + 1}"


def _action_id(ticket_id: str, run_id: str | None, order: int) -> str:
    seed = f"{ticket_id}:{run_id or 'manual'}:{order}"
    return f"act_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}"


def _expected_result(step: str) -> str:
    """从步骤文本推断预期结果：取步骤后用「，/；/。/ ：」分隔的首段，最长 256 字符。"""
    heads = re.split(r"[，、；。！？:：]", step, maxsplit=1)
    head = heads[0].strip() if heads else step.strip()
    if not head:
        head = step.strip()[:256]
    return head[:256]
