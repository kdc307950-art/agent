"""LANGGraph VPN Diagnosis Agent 包：纯只读诊断 Agent（产出结构化处置命令）。

模块归属：backend/vpn。与 ResolutionCopilot 的差异：
    - 产出物不同：本 agent 输出结构化处置命令 DiagnosisCommand，而非回复草稿；
    - 数据源不同：用只读 MockVpnAdapter 隔离真实 VPN 后台，不做真实排障；
    - 升级更严：evaluate_handoff 显式把 multi_user_impact / identity_missing /
      no_evidence / low_confidence 统一转人工，并用 FORBIDDEN_COMMANDS 封死所有副作用。

子模块分工：
    - models.py      ：枚举/禁止命令/DiagnosisCommand/DiagnosisEvaluation/evaluate_handoff
    - mock_adapter.py：只读 Mock VPN 适配器（dict/JSON 可配置）
    - tools.py       ：6 个只读 @tool + VPN_TOOLS
    - agent.py       ：VpnDiagnosisAgent 有界只读工具循环
    - service.py     ：VpnDiagnosisService 上下文组装 + 诊断门禁
    - executor.py    ：业务层校验/执行（命令→状态机映射，禁止命令零副作用）
"""

from backend.tool_governance import VPN_DIAGNOSIS_TOOLS

from .agent import (
    DIAGNOSIS_MAX_ROUNDS,
    DIAGNOSIS_MAX_TOOL_CALLS,
    DIAGNOSIS_MAX_TOOL_CALLS_PER_ROUND,
    DIAGNOSIS_SINGLE_TOOL_TIMEOUT_SECONDS,
    DIAGNOSIS_TOTAL_TIMEOUT_SECONDS,
    DiagnosisLimits,
    VpnDiagnosisAgent,
)
from .approval import (
    ALLOWED_EXEC_ACTIONS,
    ApprovalStatus,
    ReissueAction,
    ReissueActionRequest,
    ReissueExecutionResult,
    ReissuePreflightResult,
    ReissueRegistry,
    action_allowed,
    approve_reissue,
    build_reissue_idempotency_key,
    build_reissue_request_from_payload,
    cancel_reissue,
    execute_approved_reissue,
    preflight_reissue,
    reject_reissue,
    start_reissue_approval,
)
from .closed_loop import (
    VpnClosedLoopService,
    VpnCustomerActionError,
    VpnDiagnosisNotFound,
)
from .diagnosis import (
    CustomerActionStatus,
    DiagnosisRegistry,
    DiagnosisRunStatus,
    EscalationStatus,
    VpnCustomerAction,
    VpnCustomerActionResult,
    VpnDiagnosisFinding,
    VpnDiagnosisRun,
    VpnEscalation,
    parse_steps_to_actions,
)
from .executor import (
    COMMAND_TO_ACTION,
    HUMAN_QUEUE_TEAM_ID,
    DiagnosisCommandRejected,
    DiagnosisCommandValidator,
    ExecResult,
    execute_diagnosis_command,
)
from .mock_adapter import MockVpnAdapter, VpnAdapter, VpnDataSource
from .models import (
    ALLOWED_COMMANDS,
    DEFAULT_HANDOFF_CONFIDENCE,
    FORBIDDEN_COMMANDS,
    DiagnosisCommand,
    DiagnosisCommandType,
    DiagnosisEvaluation,
    DiagnosisRequest,
    HandoffReason,
    evaluate_handoff,
)
from .repository import VpnDiagnosisRepository
from .rules import (
    AccountStatus,
    EvidenceDiagnosis,
    GatewayStatus,
    VpnEvidence,
    build_evidence_from_case,
    evaluate_evidence,
    evaluate_evidence_handoff,
    hypothesis_code_from_hint,
    is_account_lockout_text,
    is_version_outdated,
    normalize_account_status,
    normalize_gateway_status,
)
from .runtime_view import RuntimeView
from .sandbox_adapter import (
    ALLOW_ALL_ACCESS,
    AccessPolicy,
    CallAudit,
    CircuitBreaker,
    CircuitBreakerConfig,
    ResilienceConfig,
    RetryPolicy,
    SandboxVpnAdapter,
    TenantScopePolicy,
    VpnDataSourceTier,
    VpnResilientAdapter,
    build_vpn_adapter,
    desensitize,
    redact,
)
from .service import VpnDiagnosisService
from .tools import VPN_REISSUE_TOOLS, VPN_TOOLS, reissue_vpn_config

__all__ = [
    "ALLOW_ALL_ACCESS",
    "ALLOWED_COMMANDS",
    "ALLOWED_EXEC_ACTIONS",
    "AccessPolicy",
    "AccountStatus",
    "ApprovalStatus",
    "CallAudit",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "COMMAND_TO_ACTION",
    "ReissueAction",
    "ReissueActionRequest",
    "ReissueExecutionResult",
    "ReissuePreflightResult",
    "ReissueRegistry",
    "VPN_REISSUE_TOOLS",
    "action_allowed",
    "approve_reissue",
    "build_reissue_idempotency_key",
    "build_reissue_request_from_payload",
    "cancel_reissue",
    "execute_approved_reissue",
    "preflight_reissue",
    "reject_reissue",
    "reissue_vpn_config",
    "start_reissue_approval",
    "DEFAULT_HANDOFF_CONFIDENCE",
    "DIAGNOSIS_MAX_ROUNDS",
    "DIAGNOSIS_MAX_TOOL_CALLS",
    "DIAGNOSIS_MAX_TOOL_CALLS_PER_ROUND",
    "DIAGNOSIS_SINGLE_TOOL_TIMEOUT_SECONDS",
    "DIAGNOSIS_TOTAL_TIMEOUT_SECONDS",
    "DiagnosisCommand",
    "DiagnosisCommandRejected",
    "DiagnosisCommandType",
    "DiagnosisCommandValidator",
    "DiagnosisEvaluation",
    "DiagnosisLimits",
    "DiagnosisRegistry",
    "DiagnosisRequest",
    "DiagnosisRunStatus",
    "CustomerActionStatus",
    "EscalationStatus",
    "EvidenceDiagnosis",
    "ExecResult",
    "FORBIDDEN_COMMANDS",
    "GatewayStatus",
    "HUMAN_QUEUE_TEAM_ID",
    "HandoffReason",
    "MockVpnAdapter",
    "ResilienceConfig",
    "RetryPolicy",
    "RuntimeView",
    "SandboxVpnAdapter",
    "TenantScopePolicy",
    "VPN_DIAGNOSIS_TOOLS",
    "VPN_TOOLS",
    "VpnAdapter",
    "VpnClosedLoopService",
    "VpnCustomerAction",
    "VpnCustomerActionError",
    "VpnCustomerActionResult",
    "VpnDataSource",
    "VpnDataSourceTier",
    "VpnDiagnosisAgent",
    "VpnDiagnosisFinding",
    "VpnDiagnosisNotFound",
    "VpnDiagnosisRepository",
    "VpnDiagnosisRun",
    "VpnDiagnosisService",
    "VpnEscalation",
    "VpnEvidence",
    "VpnResilientAdapter",
    "build_evidence_from_case",
    "build_vpn_adapter",
    "desensitize",
    "evaluate_evidence",
    "redact",
    "evaluate_evidence_handoff",
    "evaluate_handoff",
    "execute_diagnosis_command",
    "hypothesis_code_from_hint",
    "is_account_lockout_text",
    "is_version_outdated",
    "normalize_account_status",
    "normalize_gateway_status",
    "parse_steps_to_actions",
]
