"""重新下发 VPN 配置文件（reissue_vpn_config）的低风险审批式执行链路。

模块归属：backend/vpn。设计目标（对齐 docs/product/vpn-diagnosis-agent-contract.md 的
「只诊断、不自动执行」边界 + 用户对「审批式执行动作」的六项硬验收）：

    1. 领域模型：ReissueActionRequest / ReissuePreflightResult / ReissueExecutionResult，
       全部 extra="forbid"，拒绝模型/调用方自由注入字段。
    2. 前置校验：preflight_reissue() 确定性纯逻辑（账号 active / 资产归属匹配 /
       客户端配置版本可读 / 租户匹配），任一不过则拒绝并返回结构化 fail_reasons。
    3. 幂等键：build_reissue_idempotency_key() 生成确定性键；执行前先查
       ReissueRegistry（内存登记表，生产可换 workflow_operation / audit 持久化），
       同键已交付/确认则直接返回既有结果，不重复执行（复用 interrupt_id + 乐观锁语义）。
    4. 审批人：start_reissue_approval() 登记 PENDING 并发起审批；approve_reissue() 由
       human-in-the-loop /chat/resume 恢复通道调用，记录 approver_user_id 并置 APPROVED。
    5. 操作日志：发起/校验失败/审批通过/审批拒绝/取消/执行成功/执行失败/回滚接管
       共多个阶段写 audit.record_event（vpn_reissue_*），payload 关联
       tenant/user/asset/ticket/approver/idempotency_key/result。
    6. 执行与结果确认：execute_approved_reissue() 是唯一受控执行入口——
       仅当 registry 状态为 APPROVED 且前置校验通过且幂等键未执行过才真实下发；
       执行后记录 delivered/confirmed（delivered=已下发；confirmed=已确认）。
       失败可定位（error_code/reason）、可重试（幂等键）、可人工接管（status=FAILED）。

红线（绝不自动执行）：reset_password / unlock_account / grant_vpn_permission /
modify_vpn_config / restart_gateway / close_ticket / send_customer_message 仍由
backend/vpn/models.FORBIDDEN_COMMANDS 与 executor 拒绝，本模块只负责 reissue_vpn_config
这一「配置交付」类动作，且必须经审批 + 幂等 + 前置校验三重门禁。

设计取向：审批授权以 ReissueRegistry 的 APPROVED 状态为准（而非信任工具入参或模型自述），
即使模型或调用方绕过治理直接调用 execute_approved_reissue，只要该幂等键未真正经过
approve_reissue() 置 APPROVED，就返回 not_approved，绝不产生副作用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("langgraph.vpn")


# ===========================================================================
# 枚举
# ===========================================================================


class ReissueAction(StrEnum):
    """可审批执行的 VPN 配置交付动作（当前仅 reissue_vpn_config）。"""

    REISSUE_VPN_CONFIG = "reissue_vpn_config"


class ApprovalStatus(StrEnum):
    """一次审批式执行动作的生命周期状态（区别于工单 TicketStatus，属 approval 域）。"""

    PENDING = "pending"      # 已发起审批，等待审批人
    APPROVED = "approved"    # 审批通过
    REJECTED = "rejected"    # 审批拒绝
    CANCELLED = "cancelled"  # 取消（人工接管/撤销）
    FAILED = "failed"        # 前置校验失败或执行失败（可重试/人工接管）
    DELIVERED = "delivered"  # 配置已下发
    CONFIRMED = "confirmed"  # 下发并确认


# ===========================================================================
# 领域模型（extra="forbid"）
# ===========================================================================


class ReissueActionRequest(BaseModel):
    """一次「重新下发 VPN 配置文件」执行动作的不可变请求快照。

    仅携带执行所必需的只读上下文；租户/坐席/审批人身份由 RunContext 与
    approver_user_id 提供，不进入本模型（避免信任请求体注入身份）。
    """

    model_config = ConfigDict(extra="forbid")

    action: ReissueAction = ReissueAction.REISSUE_VPN_CONFIG
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    asset_id: str | None = Field(default=None, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    # 目标配置版本（前置校验必须能从 Mock 数据源读到，否则拒绝）
    client_version: str = Field(min_length=1, max_length=64)
    # 审批人（approve_reissue 时回填；审计用）
    approver_user_id: str | None = Field(default=None, max_length=128)
    # 幂等键（防重复审批/重复执行）；未给则由 build_reissue_idempotency_key 生成
    idempotency_key: str = Field(min_length=1, max_length=256)
    # 乐观锁（工单 expected_version 快照）
    expected_version: int = Field(ge=0)
    reason_codes: list[str] = Field(default_factory=list, max_length=32)

    @property
    def scope_id(self) -> str:
        """幂等/审计用稳定主键（tenant:user:asset:ticket）。"""
        return f"{self.tenant_id}:{self.user_id}:{self.asset_id or '-'}:{self.ticket_id}"


class ReissuePreflightResult(BaseModel):
    """前置校验结果（确定性判定，供审计与拒绝原因展示）。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    fail_reasons: list[str] = Field(default_factory=list, max_length=32)
    account_status: str | None = None
    asset_owner_user_id: str | None = None
    asset_status: str | None = None
    client_config_version: str | None = None
    tenant_match: bool = False


class ReissueExecutionResult(BaseModel):
    """一次审批式执行的最终结果（delivered=已下发；confirmed=已确认）。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    action: ReissueAction = ReissueAction.REISSUE_VPN_CONFIG
    status: ApprovalStatus
    idempotency_key: str
    delivered: bool = False
    confirmed: bool = False
    reason: str | None = None
    error_code: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# ===========================================================================
# 幂等键
# ===========================================================================


def build_reissue_idempotency_key(
    *,
    tenant_id: str,
    user_id: str,
    asset_id: str | None,
    ticket_id: str,
    client_version: str,
) -> str:
    """生成确定性幂等键：{tenant}:{user}:{asset}:{ticket}:{version}。

    同一键重复出现表示「同一目标配置交付」，应返回既有结果而不重复执行。
    """
    return (
        f"reissue:{tenant_id}:{user_id}:{asset_id or '-'}:{ticket_id}:{client_version}"
    )


# ===========================================================================
# 执行动作白名单（触发入口 = request_approval + payload.action）
# ===========================================================================

# 允许在审批通过后执行的交付类动作。当前仅「重新下发标准客户端配置」。
# 注意：modify_vpn_config（改服务器/网关/防火墙配置）仍是 FORBIDDEN 红线，绝不在此白名单内。
ALLOWED_EXEC_ACTIONS: frozenset[str] = frozenset({"reissue_vpn_config"})


def action_allowed(action: str | None) -> bool:
    """执行动作白名单校验：仅允许标准配置交付类动作，其它一律拒绝。"""
    return action in ALLOWED_EXEC_ACTIONS


def build_reissue_request_from_payload(
    payload: Any,
    *,
    tenant_id: str,
    expected_version: int = 0,
) -> ReissueActionRequest:
    """从 request_approval 命令的 payload 解析出 reissue 执行请求（并先行白名单校验）。

    payload 约定（触发入口固定基线）：{"action": "reissue_vpn_config", "user_id": ...,
    "asset_id": ..., "ticket_id": ..., "target_version": ...}。tenant_id 取自 RunContext
    （权威来源，不信任请求体），故本函数显式接收 tenant_id。
    返回：ReissueActionRequest；若 action 不在白名单或缺少必填字段，抛 ValueError。
    """
    if isinstance(payload, dict):
        payload = dict(payload)
    else:
        payload = {}

    action = str(payload.get("action") or "")
    if not action_allowed(action):
        raise ValueError(f"非允许的执行动作（红线拒绝）: {action or '<空>'}")

    user_id = str(payload.get("user_id") or "")
    ticket_id = str(payload.get("ticket_id") or "")
    target_version = str(payload.get("target_version") or payload.get("client_version") or "")
    asset_id = payload.get("asset_id") or None
    if not user_id or not ticket_id or not target_version:
        raise ValueError("reissue 动作缺少 user_id/ticket_id/target_version")

    key = str(payload.get("idempotency_key") or "") or build_reissue_idempotency_key(
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=target_version,
    )
    return ReissueActionRequest(
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=target_version,
        idempotency_key=key,
        expected_version=expected_version,
        reason_codes=list(payload.get("reason_codes") or []),
    )


# ===========================================================================
# 前置校验（纯逻辑，可单测）
# ===========================================================================


async def preflight_reissue(*, runtime: Any, tenant_id: str, user_id: str, asset_id: str | None) -> ReissuePreflightResult:
    """四项前置校验，任一不过则 ok=False 且不给授权：

    1. account_active   ：runtime.vpn_adapter.get_account_status(user_id) 命中且 status==active；
    2. asset_ownership  ：runtime.assets.get(tenant_id, asset_id) 命中、未软删、owner_user_id==user_id、
                          状态合法（未 retired）；
    3. config_version   ：runtime.vpn_adapter.get_client_config_version(user_id) 命中（能读到版本）；
    4. tenant_match     ：tenant_id 非空（scope/租户匹配由调用方 RunContext 保证，此处校验非空）。
    """
    fail_reasons: list[str] = []
    account_status: str | None = None
    asset_owner: str | None = None
    asset_status: str | None = None
    config_version: str | None = None

    adapter = getattr(runtime, "vpn_adapter", None)
    assets = getattr(runtime, "assets", None)

    # 1) 账号状态 active
    if adapter is None or not hasattr(adapter, "get_account_status"):
        fail_reasons.append("adapter_missing")
    else:
        acct = await adapter.get_account_status(user_id)
        account_status = str(acct.get("status") or "") if isinstance(acct, dict) else ""
        if not (isinstance(acct, dict) and acct.get("found") and account_status == "active"):
            fail_reasons.append("account_not_active")

    # 2) 资产归属匹配
    if assets is None or not asset_id:
        fail_reasons.append("asset_missing_or_unconfigured")
    else:
        try:
            asset = await assets.get(tenant_id, asset_id)
        except Exception as exc:  # 资产域查询异常不得阻断审批前校验（作为拒绝原因）
            logger.warning("资产查询失败 asset=%s: %s", asset_id, type(exc).__name__)
            asset = None
        if asset is None:
            fail_reasons.append("asset_not_found")
        else:
            asset_owner = getattr(asset, "owner_user_id", None)
            asset_status = str(getattr(asset, "status", None) or "")
            is_deleted = bool(getattr(asset, "is_deleted", False))
            if is_deleted:
                fail_reasons.append("asset_deleted")
            elif asset_owner != user_id:
                fail_reasons.append("asset_owner_mismatch")
            elif asset_status == "retired":
                fail_reasons.append("asset_retired")

    # 3) 客户端配置版本可读
    if adapter is None or not hasattr(adapter, "get_client_config_version"):
        fail_reasons.append("config_version_unavailable")
    else:
        cfg = await adapter.get_client_config_version(user_id)
        config_version = str(cfg.get("version") or "") if isinstance(cfg, dict) else ""
        if not (isinstance(cfg, dict) and cfg.get("found") and config_version):
            fail_reasons.append("config_version_unavailable")

    # 4) 租户匹配
    tenant_match = bool(tenant_id)
    if not tenant_match:
        fail_reasons.append("tenant_missing")

    return ReissuePreflightResult(
        ok=not fail_reasons,
        fail_reasons=fail_reasons,
        account_status=account_status,
        asset_owner_user_id=asset_owner,
        asset_status=asset_status,
        client_config_version=config_version,
        tenant_match=tenant_match,
    )


# ===========================================================================
# 幂等/审批状态登记表
# ===========================================================================


@dataclass
class ReissueRegistry:
    """审批式执行动作的幂等/状态登记表（内存实现）。

    生产环境应把该表映射到 workflow_operation（operation_id 幂等 + committed 终态）
    与 audit 事件（审计留痕）；此处提供支持单元测试的内存版，保证「同键重复
    不重复执行」「未 APPROVED 不执行」两个硬验收在纯函数层可验证。
    """

    _status: dict[str, ApprovalStatus] = field(default_factory=dict)
    _result: dict[str, ReissueExecutionResult] = field(default_factory=dict)

    def get_status(self, idempotency_key: str) -> ApprovalStatus | None:
        return self._status.get(idempotency_key)

    def set_status(self, idempotency_key: str, status: ApprovalStatus) -> None:
        self._status[idempotency_key] = status

    def get_result(self, idempotency_key: str) -> ReissueExecutionResult | None:
        return self._result.get(idempotency_key)

    def set_result(self, idempotency_key: str, result: ReissueExecutionResult) -> None:
        self._result[idempotency_key] = result
        self._status[idempotency_key] = result.status


# 模块级默认登记表（测试隔离可注入独立实例）
_DEFAULT_REGISTRY = ReissueRegistry()

# 公共别名：供 VpnReissueService 与 reissue_vpn_config 工具共享同一审批状态登记表，
# 保证「工具」与「服务」两个执行入口看到同一个 APPROVED / 终态，消除双入口不一致。
DEFAULT_REISSUE_REGISTRY = _DEFAULT_REGISTRY


# ===========================================================================
# 审计辅助
# ===========================================================================


async def _audit(
    runtime: Any,
    run_context: Any,
    event_type: str,
    *,
    status: str,
    payload: dict[str, Any],
) -> None:
    """写审计；失败不阻断主流程。payload 已由调用方关联 tenant/user/approver 等。"""
    audit = getattr(runtime, "audit", None)
    if audit is None or run_context is None:
        return
    try:
        await audit.record_event(run_context, event_type, status=status, payload=payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("VPN 审批审计写入失败 %s: %s", event_type, type(exc).__name__)


def _request_id(request: ReissueActionRequest) -> dict[str, Any]:
    return {
        "action": request.action.value,
        "tenant_id": request.tenant_id,
        "user_id": request.user_id,
        "asset_id": request.asset_id,
        "ticket_id": request.ticket_id,
        "client_version": request.client_version,
        "idempotency_key": request.idempotency_key,
        "approver_user_id": request.approver_user_id,
    }


# ===========================================================================
# 发起审批 → 审批（批准/拒绝/取消）→ 执行（受控入口）
# ===========================================================================


async def start_reissue_approval(
    *,
    runtime: Any,
    run_context: Any,
    tenant_id: str,
    user_id: str,
    ticket_id: str,
    client_version: str,
    asset_id: str | None = None,
    idempotency_key: str | None = None,
    expected_version: int = 0,
    reason_codes: list[str] | None = None,
    registry: ReissueRegistry | None = None,
) -> dict[str, Any]:
    """发起一次 reissue_vpn_config 审批。

    流程：生成幂等键 → 幂等（同键已交付/确认则直接返回既有结果）→ 前置校验 →
        通过则登记 PENDING 并审计 vpn_reissue_started；不过则登记 FAILED 并审计
        vpn_reissue_preflight_failed，返回拒绝原因。

    返回：{"request", "preflight", "status", "idempotency_key"}（供审批卡/审计）。
    """
    registry = registry or _DEFAULT_REGISTRY
    key = idempotency_key or build_reissue_idempotency_key(
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=client_version,
    )

    request = ReissueActionRequest(
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=client_version,
        idempotency_key=key,
        expected_version=expected_version,
        reason_codes=list(reason_codes or []),
    )

    # 幂等：同键已交付/确认 → 直接返回既有结果，不重复发起/执行
    existing = registry.get_result(key)
    if existing is not None and (existing.delivered or existing.status in (ApprovalStatus.DELIVERED, ApprovalStatus.CONFIRMED)):
        return {
            "request": request.model_dump(mode="json"),
            "preflight": ReissuePreflightResult(ok=True).model_dump(mode="json"),
            "status": existing.status.value,
            "idempotency_key": key,
            "existing_result": existing.model_dump(mode="json"),
        }

    preflight = await preflight_reissue(
        runtime=runtime, tenant_id=tenant_id, user_id=user_id, asset_id=asset_id
    )
    if not preflight.ok:
        registry.set_status(key, ApprovalStatus.FAILED)
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_preflight_failed",
            status="failed",
            payload={
                **_request_id(request),
                "fail_reasons": preflight.fail_reasons,
            },
        )
        return {
            "request": request.model_dump(mode="json"),
            "preflight": preflight.model_dump(mode="json"),
            "status": ApprovalStatus.FAILED.value,
            "idempotency_key": key,
            "rejected": True,
            "fail_reasons": preflight.fail_reasons,
        }

    registry.set_status(key, ApprovalStatus.PENDING)
    await _audit(
        runtime,
        run_context,
        "vpn_reissue_started",
        status="pending",
        payload={
            **_request_id(request),
            "preflight": preflight.model_dump(mode="json"),
        },
    )
    return {
        "request": request.model_dump(mode="json"),
        "preflight": preflight.model_dump(mode="json"),
        "status": ApprovalStatus.PENDING.value,
        "idempotency_key": key,
        "approval_required": True,
    }


async def approve_reissue(
    *,
    request: ReissueActionRequest,
    runtime: Any,
    run_context: Any,
    approver_user_id: str,
    registry: ReissueRegistry | None = None,
) -> ReissueExecutionResult:
    """审批人批准：记录 approver_user_id → 置 APPROVED → 进入受控执行。

    幂等：同键已交付/确认则直接返回既有结果，不重复执行（重复审批不重复执行）。
    """
    registry = registry or _DEFAULT_REGISTRY
    key = request.idempotency_key
    existing = registry.get_result(key)
    if existing is not None and (existing.delivered or existing.status in (ApprovalStatus.DELIVERED, ApprovalStatus.CONFIRMED)):
        return existing

    # 风险 F：审批人 scope 校验（外部 /chat/resume 已强制 chat:approve；模块层纵深 defense-in-depth）。
    scopes = set(getattr(run_context, "scopes", frozenset()) or ())
    if "ticket:approve" not in scopes and "chat:approve" not in scopes:
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_denied",
            status="denied",
            payload={**_request_id(request), "reason": "missing_approve_scope"},
        )
        return ReissueExecutionResult(
            ok=False,
            status=ApprovalStatus.PENDING,
            idempotency_key=key,
            reason="缺少审批权限",
            error_code="denied_scope",
        )

    approved = request.model_copy(update={"approver_user_id": approver_user_id})
    registry.set_status(key, ApprovalStatus.APPROVED)
    await _audit(
        runtime,
        run_context,
        "vpn_reissue_approved",
        status="approved",
        payload={**_request_id(approved)},
    )
    return await execute_approved_reissue(
        request=approved, runtime=runtime, run_context=run_context, registry=registry
    )


async def reject_reissue(
    *,
    request: ReissueActionRequest,
    runtime: Any,
    run_context: Any,
    approver_user_id: str | None = None,
    reason: str = "审批拒绝",
    registry: ReissueRegistry | None = None,
) -> ReissueExecutionResult:
    """审批人拒绝/取消：登记 REJECTED（人工接管），审计 vpn_reissue_rejected。"""
    registry = registry or _DEFAULT_REGISTRY
    registry.set_status(request.idempotency_key, ApprovalStatus.REJECTED)
    await _audit(
        runtime,
        run_context,
        "vpn_reissue_rejected",
        status="rejected",
        payload={
            **_request_id(request.model_copy(update={"approver_user_id": approver_user_id})),
            "reason": reason,
        },
    )
    return ReissueExecutionResult(
        ok=False,
        status=ApprovalStatus.REJECTED,
        idempotency_key=request.idempotency_key,
        reason=reason,
        error_code="rejected",
    )


async def cancel_reissue(
    *,
    request: ReissueActionRequest,
    runtime: Any,
    run_context: Any,
    approver_user_id: str | None = None,
    reason: str = "操作取消",
    registry: ReissueRegistry | None = None,
) -> ReissueExecutionResult:
    """取消：登记 CANCELLED（人工接管），审计 vpn_reissue_cancelled（带审批人）。"""
    registry = registry or _DEFAULT_REGISTRY
    registry.set_status(request.idempotency_key, ApprovalStatus.CANCELLED)
    await _audit(
        runtime,
        run_context,
        "vpn_reissue_cancelled",
        status="cancelled",
        payload={
            **_request_id(request.model_copy(update={"approver_user_id": approver_user_id})),
            "reason": reason,
        },
    )
    return ReissueExecutionResult(
        ok=False,
        status=ApprovalStatus.CANCELLED,
        idempotency_key=request.idempotency_key,
        reason=reason,
        error_code="cancelled",
    )


async def execute_approved_reissue(
    *,
    request: ReissueActionRequest,
    runtime: Any,
    run_context: Any,
    registry: ReissueRegistry | None = None,
    execute: Any | None = None,
) -> ReissueExecutionResult:
    """受控执行入口（唯一能触发真实下发的函数）。

    门禁严格顺序（任一不过即拒绝/失败，零副作用）：
        0) 幂等：同键已交付/确认 → 直接返回既有结果；
        1) 授权：registry 状态必须为 APPROVED（只有 approve_reissue 能置），否则 not_approved；
        2) 前置校验：preflight_reissue 必须通过；
        3) 真实下发：经 execute（缺省 runtime.vpn_adapter.reissue_config）执行；
        4) 结果确认：delivered / confirmed 落 audit.vpn_reissue_executed；
           失败落 audit.vpn_reissue_failed 并置 FAILED（可重试/人工接管）。
    """
    registry = registry or _DEFAULT_REGISTRY
    key = request.idempotency_key
    base_payload = _request_id(request)

    existing = registry.get_result(key)
    if existing is not None and (existing.delivered or existing.status in (ApprovalStatus.DELIVERED, ApprovalStatus.CONFIRMED)):
        return existing

    status = registry.get_status(key)
    if status != ApprovalStatus.APPROVED:
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_denied",
            status="denied",
            payload={**base_payload, "reason": "not_approved", "current_status": status.value if status else None},
        )
        return ReissueExecutionResult(
            ok=False,
            status=status or ApprovalStatus.PENDING,
            idempotency_key=key,
            reason="该动作尚未获批准，拒绝执行",
            error_code="not_approved",
        )

    preflight = await preflight_reissue(
        runtime=runtime,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        asset_id=request.asset_id,
    )
    if not preflight.ok:
        registry.set_status(key, ApprovalStatus.FAILED)
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_preflight_failed",
            status="failed",
            payload={**base_payload, "fail_reasons": preflight.fail_reasons},
        )
        return ReissueExecutionResult(
            ok=False,
            status=ApprovalStatus.FAILED,
            idempotency_key=key,
            reason="前置校验未通过",
            error_code="preflight_failed",
            detail={"fail_reasons": preflight.fail_reasons},
        )

    runner = execute or _default_execute_reissue
    # 风险 E：独立「执行开始」事件（验收 #3 要 execution_started/completed/failed 三态）。
    await _audit(
        runtime,
        run_context,
        "vpn_reissue_execution_started",
        status="running",
        payload={**base_payload},
    )
    try:
        outcome = await runner(runtime, user_id=request.user_id, idempotency_key=key, target_version=request.client_version)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reissue 执行失败 key=%s: %s", key, type(exc).__name__)
        registry.set_status(key, ApprovalStatus.FAILED)
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_failed",
            status="failed",
            payload={**base_payload, "error_type": type(exc).__name__, "reason": "execute_failed"},
        )
        return ReissueExecutionResult(
            ok=False,
            status=ApprovalStatus.FAILED,
            idempotency_key=key,
            reason="执行失败，可重试或人工接管",
            error_code="execute_failed",
            detail={"error_type": type(exc).__name__},
        )

    if not isinstance(outcome, dict):
        outcome = {}
    delivered = bool(outcome.get("delivered", False))
    confirmed = bool(outcome.get("confirmed", delivered))
    error_code = outcome.get("error_code")
    if error_code or not delivered:
        registry.set_status(key, ApprovalStatus.FAILED)
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_failed",
            status="failed",
            payload={**base_payload, "result": outcome, "reason": outcome.get("reason") or "reissue_failed"},
        )
        return ReissueExecutionResult(
            ok=False,
            status=ApprovalStatus.FAILED,
            idempotency_key=key,
            reason=outcome.get("reason") or "重新下发失败",
            error_code=str(error_code) if error_code else "reissue_failed",
            detail=outcome,
        )

    final_status = ApprovalStatus.CONFIRMED if confirmed else ApprovalStatus.DELIVERED
    result = ReissueExecutionResult(
        ok=True,
        status=final_status,
        idempotency_key=key,
        delivered=delivered,
        confirmed=confirmed,
        detail=outcome,
    )
    registry.set_result(key, result)
    await _audit(
        runtime,
        run_context,
        "vpn_reissue_executed",
        status="completed",
        payload={**base_payload, "result": result.model_dump(mode="json")},
    )
    return result


# 缺省真实下发执行器：经 runtime.vpn_adapter.reissue_config（Mock/真实沙箱）。
async def _default_execute_reissue(runtime: Any, *, user_id: str, idempotency_key: str, target_version: str) -> dict[str, Any]:
    adapter = getattr(runtime, "vpn_adapter", None)
    if adapter is None or not hasattr(adapter, "reissue_config"):
        raise RuntimeError("vpn_adapter 未配置 reissue_config，无法执行下发")
    return await adapter.reissue_config(
        user_id=user_id, idempotency_key=idempotency_key, target_version=target_version
    )


# ===========================================================================
# 触发入口分派：request_approval + payload.action -> 受控执行动作
# ===========================================================================


async def dispatch_approved_action(
    *,
    command: Any,
    runtime: Any,
    run_context: Any,
    registry: ReissueRegistry | None = None,
) -> dict[str, Any]:
    """从一条 request_approval 诊断命令分派到对应受控执行动作。

    触发入口固定基线：DiagnosisCommand.command == "request_approval"，payload.action 指定动作。
    仅当 action 在白名单内（当前仅 reissue_vpn_config）且 payload 完整时发起审批（登记 PENDING
    + 前置校验 + 审计）；action 非白名单（如 modify_vpn_config 等红线）或字段缺失时审计并拒绝，
    绝不发起执行。
    """
    if not isinstance(command, dict):
        return {"dispatched": False, "status": "skipped", "reason": "no_command"}
    if str(command.get("command")) != "request_approval":
        return {"dispatched": False, "status": "skipped", "reason": "not_request_approval"}

    tenant_id = getattr(run_context, "tenant_id", None)
    if not tenant_id:
        return {
            "dispatched": False,
            "status": "failed",
            "reason": "missing_tenant",
            "error_code": "denied_tenant",
        }

    try:
        request = build_reissue_request_from_payload(
            command.get("payload") or {}, tenant_id=tenant_id
        )
    except ValueError as exc:
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_denied",
            status="denied",
            payload={"command": str(command.get("command")), "reason": str(exc)},
        )
        return {
            "dispatched": False,
            "status": "denied",
            "reason": str(exc),
            "error_code": "forbidden_action",
        }

    outcome = await start_reissue_approval(
        runtime=runtime,
        run_context=run_context,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        asset_id=request.asset_id,
        ticket_id=request.ticket_id,
        client_version=request.client_version,
        idempotency_key=request.idempotency_key,
        expected_version=request.expected_version,
        reason_codes=request.reason_codes,
        registry=registry,
    )
    return {"dispatched": True, **outcome}
