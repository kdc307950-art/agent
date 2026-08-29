"""工单持久化包：对外统一导出模型、仓储与异常类型。

子模块分工：
    - models.py     ：Pydantic 数据模型（建单入参 / 工单记录 / 状态流水 / 入站事件结果）
    - repository.py ：工单主仓储（乐观锁流转、幂等建单、渠道事件、工作流运行登记）
    - operations.py ：周边运营数据（Outbox、SLA 实例、满意度调查、概览聚合）
    - sla.py        ：业务日历 SLA 计算
    - routing.py    ：路由规则与最空闲在岗坐席选择
    - policies.py   ：租户 IT 策略（必填字段、自动回答/审批开关、SLA 引用）
"""

from .models import CreateTicket, InboundEventResult, TicketRecord, TicketStatusEvent
from .operations import OperationsConflict, TicketOperationsRepository, sla_policy_candidates
from .policies import ItPolicyNotFound, ItPolicyRepository, TenantItPolicy, UpsertItPolicy
from .repository import (
    AssetBindingError,
    InboundEventConflict,
    TicketAlreadyExists,
    TicketCapacityExceeded,
    TicketNotFound,
    TicketRepository,
    TicketVersionConflict,
    canonical_payload_hash,
    ticket_repository_context,
)
from .routing import RoutingDecision, RoutingRepository
from .sla import BusinessCalendar

__all__ = [
    "AssetBindingError",
    "BusinessCalendar",
    "CreateTicket",
    "InboundEventConflict",
    "InboundEventResult",
    "ItPolicyNotFound",
    "ItPolicyRepository",
    "OperationsConflict",
    "RoutingDecision",
    "RoutingRepository",
    "TenantItPolicy",
    "TicketAlreadyExists",
    "TicketCapacityExceeded",
    "TicketNotFound",
    "TicketOperationsRepository",
    "TicketRecord",
    "TicketRepository",
    "TicketStatusEvent",
    "TicketVersionConflict",
    "UpsertItPolicy",
    "canonical_payload_hash",
    "sla_policy_candidates",
    "ticket_repository_context",
]
