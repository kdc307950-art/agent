"""工单持久化模型（Pydantic）。

职责：
    - CreateTicket：建单请求的入参模型，带字段约束（租户内唯一 ticket_id、渠道、优先级等）
    - TicketRecord：tickets 表一行的完整投影，供仓储层返回给上层
    - TicketStatusEvent：状态流转流水（审计/历史回放用）
    - InboundEventResult：渠道入站事件登记结果（快速 ACK 阶段）

关键设计：
    - 全部 model_config = ConfigDict(extra="forbid")：拒绝未知字段，防止调用方传错字段被静默吞掉
    - TicketRecord/TicketStatusEvent 额外 frozen=True：不可变，防止运行中意外篡改状态快照
    - 状态枚举（TicketStatus）与动作枚举（ActorType）复用于 src.my_agent.helpdesk，
      保证受理图与持久化层对同一状态机的认知一致
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.my_agent.helpdesk import ActorType, TicketStatus


class CreateTicket(BaseModel):
    """建单请求入参：字段级约束 + 拒绝未知字段。

    ticket_id 由调用方（受理流程）生成而不是数据库自增，便于渠道事件幂等映射；
    actor_type/actor_id 记录「谁发起的建单」（客户或坐席），用于权限与审计。
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    requester_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    channel: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    external_ticket_id: str | None = Field(default=None, min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=8_000)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    asset_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class TicketRecord(BaseModel):
    """工单记录（tickets 表一行）。

    frozen=True：作为并发控制的结果快照返回，调用方不可原地修改；
    version 是乐观锁版本号，状态流转必须携带匹配的 expected_version。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    ticket_id: str
    requester_id: str
    channel: str
    external_ticket_id: str | None
    title: str
    description: str
    status: TicketStatus
    priority: str
    category: str | None
    asset_id: str | None
    assigned_team_id: str | None
    assigned_user_id: str | None
    version: int
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None


class TicketStatusEvent(BaseModel):
    """状态流转流水条目（ticket_status_events 表一行）。

    每次状态变更（含建单 create）都会追加一条，from_status -> to_status 完整记录
    动作、操作者与工单版本，是历史回放与审计的基础。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: int
    tenant_id: str
    ticket_id: str
    from_status: TicketStatus | None
    to_status: TicketStatus
    action: str
    actor_type: ActorType
    actor_id: str
    ticket_version: int
    payload: dict[str, Any]
    occurred_at: datetime


class InboundEventResult(BaseModel):
    """渠道入站事件登记结果（InboundWorker 快速 ACK 阶段）。

    created=True 表示本次调用真正插入了一条新事件；False 表示幂等命中已有记录。
    payload_hash 用于校验「同一渠道事件标识是否对应了不同载荷」。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    created: bool
    tenant_id: str
    channel: str
    external_event_id: str
    payload_hash: str
    ticket_id: str | None
    status: str = "received"
    attempts: int = 0


class IntakeHandoff(BaseModel):
    """Agent 1（Intake/Triage）→ Agent 2（Resolution Copilot）的结构化 Handoff 契约。

    两个 Agent 不通过自然语言传递状态，而通过本结构化工单上下文协作：
    Agent 1 受理完成后，工单记录 + workflow_runs 落库即构成该 Handoff；
    CopilotRequest 由后端从工单/消息/资产只读组装，租户与身份从 RunContext
    注入，不读取模型请求体。

    字段说明：
        tenant_id / workflow_run_id: 账本归属与受理工作流运行（审计串联）
        expected_version: 乐观锁版本，Agent 2 生成前校验工单未变
        fields: 受理阶段收集的客户字段（如 device / network / error_message）
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    workflow_run_id: str = Field(min_length=1, max_length=128)
    requester_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    subcategory: str | None = Field(default=None, max_length=128)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")
    status: str = Field(min_length=1, max_length=32)
    fields: dict[str, Any] = Field(default_factory=dict)
    asset_id: str | None = Field(default=None, max_length=64)
    dispatch_team_id: str | None = Field(default=None, max_length=128)
    expected_version: int = Field(ge=0)
