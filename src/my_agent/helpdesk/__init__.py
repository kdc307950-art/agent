"""工单领域与受理编排包：对外统一导出领域规则与受理图。

子模块分工：
    - domain.py  ：状态机（_TRANSITIONS）、动作权限、恢复命令校验（纯函数，图/API/仓储共用）
    - intake.py  ：确定性分类（关键词）、必填字段策略、派单决策（团队/优先级/风险）
    - graph.py   ：LangGraph 受理图（归一化 -> 分类 -> 策略 -> 完整性 -> 澄清/派单 -> 拟答）

用法：
    transition_ticket(status, command, scopes=...) 判状态机合法性；
    build_helpdesk_intake_graph(...) 构建可中断恢复的受理工作流。
"""

from .domain import (
    ActorType,
    InvalidTicketTransition,
    PendingTicketInterrupt,
    ResumeAction,
    ResumeCommandMismatch,
    TicketAction,
    TicketCommand,
    TicketPermissionDenied,
    TicketResumeCommand,
    TicketStatus,
    ValidatedResume,
    allowed_actions,
    assert_actor_authorized,
    required_scopes,
    transition_ticket,
    validate_resume_command,
)
from .graph import HelpdeskIntakeState, build_helpdesk_intake_graph
from .intake import (
    ClassificationResult,
    DispatchDecision,
    IntakePolicy,
    KeywordTicketClassifier,
    RiskLevel,
    TicketCategory,
    TicketClassifier,
    assess_and_dispatch,
    missing_required_fields,
    normalize_fields,
)

__all__ = [
    "ActorType",
    "ClassificationResult",
    "DispatchDecision",
    "HelpdeskIntakeState",
    "IntakePolicy",
    "InvalidTicketTransition",
    "KeywordTicketClassifier",
    "PendingTicketInterrupt",
    "ResumeAction",
    "ResumeCommandMismatch",
    "RiskLevel",
    "TicketAction",
    "TicketCategory",
    "TicketClassifier",
    "TicketCommand",
    "TicketPermissionDenied",
    "TicketResumeCommand",
    "TicketStatus",
    "ValidatedResume",
    "allowed_actions",
    "assert_actor_authorized",
    "assess_and_dispatch",
    "build_helpdesk_intake_graph",
    "missing_required_fields",
    "normalize_fields",
    "required_scopes",
    "transition_ticket",
    "validate_resume_command",
]
