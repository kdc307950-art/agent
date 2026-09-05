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

import hashlib
import json
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
    """可审批执行的 VPN 配置交付动作。"""

    REISSUE_VPN_CONFIG = "reissue_vpn_config"
    REDEPLOY_TENANT_VPN_CONFIG = "redeploy_tenant_vpn_config"


class ApprovalStatus(StrEnum):
    """一次审批式执行动作的生命周期状态（区别于工单 TicketStatus，属 approval 域）。

    可恢复状态模型（阶段五）：在既有终态之外，增补「执行中 / 结果未知 / 需对账」三态，
    用于把「外部已下发但本地未落库」「外部超时结果未知」这类不一致状态显式建模，
    交给补偿对账任务收敛，而非简单吞掉异常后误判为失败。
    """

    PENDING = "pending"  # 已发起审批，等待审批人
    APPROVED = "approved"  # 审批通过
    REJECTED = "rejected"  # 审批拒绝
    CANCELLED = "cancelled"  # 取消（人工接管/撤销）
    FAILED = "failed"  # 前置校验失败或执行失败（可重试/人工接管）
    DELIVERED = "delivered"  # 配置已下发
    CONFIRMED = "confirmed"  # 下发并确认
    EXECUTING = "executing"  # 正在执行（执行开始后、拿到外部结论前）
    EXECUTION_UNKNOWN = "execution_unknown"  # 外部结果未知（超时/无明确应答），需对账
    RECONCILIATION_REQUIRED = "reconciliation_required"  # 外部成功但本地落库失败，需对账


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
    # 旧的单用户交付需要 user_id；租户级 FMG 下发故意不携带单用户身份。
    user_id: str | None = Field(default=None, max_length=128)
    asset_id: str | None = Field(default=None, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    # 目标配置版本（前置校验必须能从 Mock 数据源读到，否则拒绝）
    client_version: str = Field(default="tenant-current", min_length=1, max_length=64)
    # 审批人（approve_reissue 时回填；审计用）
    approver_user_id: str | None = Field(default=None, max_length=128)
    # 幂等键（防重复审批/重复执行）；未给则由 build_reissue_idempotency_key 生成
    idempotency_key: str = Field(min_length=1, max_length=256)
    # 乐观锁（工单 expected_version 快照）
    expected_version: int = Field(ge=0)
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    # 租户级 preview 门禁快照；旧入口保持为空。
    target_snapshot: dict[str, Any] = Field(default_factory=dict)
    diff_snapshot: Any = None
    diff_hash: str | None = Field(default=None, max_length=128)
    desired_state_hash: str | None = Field(default=None, max_length=128)

    @property
    def scope_id(self) -> str:
        """幂等/审计用稳定主键（tenant:user:asset:ticket）。"""
        return f"{self.tenant_id}:{self.user_id or '-'}:{self.asset_id or '-'}:{self.ticket_id}"


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
    target_snapshot: dict[str, Any] = Field(default_factory=dict)
    diff_snapshot: Any = None
    diff_hash: str | None = None
    desired_state_hash: str | None = None
    preview_status: str | None = None


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
    return f"reissue:{tenant_id}:{user_id}:{asset_id or '-'}:{ticket_id}:{client_version}"


# ===========================================================================
# 执行动作白名单（触发入口 = request_approval + payload.action）
# ===========================================================================

# 允许在审批通过后执行的交付类动作。当前仅「重新下发标准客户端配置」。
# 注意：modify_vpn_config（改服务器/网关/防火墙配置）仍是 FORBIDDEN 红线，绝不在此白名单内。
ALLOWED_EXEC_ACTIONS: frozenset[str] = frozenset(
    {"reissue_vpn_config", "redeploy_tenant_vpn_config"}
)


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

    action_enum = ReissueAction(action)
    user_id = str(payload.get("user_id") or "")
    ticket_id = str(payload.get("ticket_id") or "")
    target_version = str(payload.get("target_version") or payload.get("client_version") or "")
    asset_id = payload.get("asset_id") or None
    if action_enum == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG:
        if user_id or asset_id:
            raise ValueError("租户级下发不得混入 user_id/asset_id 单员工目标")
        if not ticket_id:
            raise ValueError("租户级下发缺少 ticket_id")
        target_version = "tenant-current"
    elif not user_id or not ticket_id or not target_version:
        raise ValueError("reissue 动作缺少 user_id/ticket_id/target_version")

    key = str(payload.get("idempotency_key") or "") or (
        build_tenant_redeploy_idempotency_key(tenant_id=tenant_id, ticket_id=ticket_id)
        if action_enum == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG
        else build_reissue_idempotency_key(
            tenant_id=tenant_id,
            user_id=user_id,
            asset_id=asset_id,
            ticket_id=ticket_id,
            client_version=target_version,
        )
    )
    return ReissueActionRequest(
        action=action_enum,
        tenant_id=tenant_id,
        user_id=user_id or None,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=target_version,
        idempotency_key=key,
        expected_version=expected_version,
        reason_codes=list(payload.get("reason_codes") or []),
    )


def build_tenant_redeploy_idempotency_key(*, tenant_id: str, ticket_id: str) -> str:
    """同一租户工单的一次配置重推只生成一个稳定幂等键。"""
    return f"redeploy:{tenant_id}:{ticket_id}"


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def preflight_tenant_redeploy(
    *, runtime: Any, tenant_id: str
) -> ReissuePreflightResult:
    """租户级 FMG 只读门禁：目标映射可见且 preview 可生成。"""
    gateway = getattr(runtime, "vpn_command_gateway", None)
    if gateway is None:
        return ReissuePreflightResult(
            ok=False, fail_reasons=["fortimanager_gateway_unavailable"], tenant_match=bool(tenant_id)
        )
    try:
        target = await gateway.validate_tenant_target(tenant_id=tenant_id)
        preview = await gateway.preview_tenant_config(tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("租户级 FMG preview 失败 tenant=%s: %s", tenant_id, type(exc).__name__)
        return ReissuePreflightResult(
            ok=False,
            fail_reasons=["fortimanager_preflight_failed"],
            tenant_match=bool(tenant_id),
        )
    reasons: list[str] = []
    if not target.get("ok"):
        reasons.append(str(target.get("error_code") or "tenant_target_invalid"))
    if not preview.get("ok"):
        reasons.append(str(preview.get("error_code") or "preview_failed"))
    target_snapshot = dict(preview.get("target") or target.get("target") or {})
    diff_snapshot = preview.get("diff_snapshot")
    diff_hash = str(preview.get("diff_hash") or "") or None
    return ReissuePreflightResult(
        ok=not reasons,
        fail_reasons=reasons,
        tenant_match=bool(tenant_id),
        target_snapshot=target_snapshot,
        diff_snapshot=diff_snapshot,
        diff_hash=diff_hash,
        desired_state_hash=_stable_hash(target_snapshot) if target_snapshot else None,
        preview_status=str(preview.get("status") or "") or None,
    )


# ===========================================================================
# 前置校验（纯逻辑，可单测）
# ===========================================================================


async def preflight_reissue(
    *,
    runtime: Any,
    tenant_id: str,
    user_id: str | None,
    asset_id: str | None,
    action: ReissueAction = ReissueAction.REISSUE_VPN_CONFIG,
) -> ReissuePreflightResult:
    """四项前置校验，任一不过则 ok=False 且不给授权：

    1. account_active   ：runtime.vpn_adapter.get_account_status(user_id) 命中且 status==active；
    2. asset_ownership  ：runtime.assets.get(tenant_id, asset_id) 命中、未软删、owner_user_id==user_id、
                          状态合法（未 retired）；
    3. config_version   ：runtime.vpn_adapter.get_client_config_version(user_id) 命中（能读到版本）；
    4. tenant_match     ：tenant_id 非空（scope/租户匹配由调用方 RunContext 保证，此处校验非空）。
    """
    if action == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG:
        return await preflight_tenant_redeploy(runtime=runtime, tenant_id=tenant_id)

    if not user_id:
        return ReissuePreflightResult(
            ok=False, fail_reasons=["user_missing"], tenant_match=bool(tenant_id)
        )

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
# 审批前重校验（阶段五：读取 LIVE 数据，防 TOCTOU 竞态）
# ===========================================================================


class PreApprovalRecheck(BaseModel):
    """审批前重校验结果（阶段五：读取 LIVE 数据而非发起时快照）。

    ``fail_reasons`` 为互相区分的结构化错误码；任一非空即 ``ok=False``，
    调用方必须在任何副作用发生前拒绝推进（零自动写）。
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    fail_reasons: list[str] = Field(default_factory=list, max_length=32)
    details: dict[str, Any] = Field(default_factory=dict)


async def _recheck_tenant_redeploy(
    *,
    runtime: Any,
    tenant_id: str,
    request: ReissueActionRequest,
    operation_status: ApprovalStatus | None,
    scopes: frozenset[str] | None,
) -> PreApprovalRecheck:
    """审批时复核租户目标与 preview，避免用旧快照直接 install。"""
    reasons: list[str] = []
    details: dict[str, Any] = {}
    gateway = getattr(runtime, "vpn_command_gateway", None)
    if gateway is None:
        reasons.append("fortimanager_gateway_unavailable")
    else:
        try:
            target = await gateway.validate_tenant_target(tenant_id=tenant_id)
            preview = await gateway.preview_tenant_config(tenant_id=tenant_id)
            details["target"] = target
            details["preview"] = preview
            approved_hash = request.diff_hash
            current_hash = preview.get("diff_hash")
            if not target.get("ok"):
                reasons.append(str(target.get("error_code") or "tenant_target_invalid"))
            if not preview.get("ok"):
                reasons.append(str(preview.get("error_code") or "preview_failed"))
            if not approved_hash or current_hash != approved_hash:
                reasons.append("preview_changed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("租户级 FMG 审批复核失败 tenant=%s: %s", tenant_id, type(exc).__name__)
            reasons.append("fortimanager_precheck_failed")

    tickets = getattr(runtime, "tickets", None)
    if tickets is None or not hasattr(tickets, "get"):
        reasons.append("ticket_unavailable")
    else:
        try:
            ticket = await tickets.get(tenant_id, request.ticket_id)
        except Exception:  # noqa: BLE001
            ticket = None
        if ticket is None:
            reasons.append("ticket_not_found")
        else:
            live_version = int(getattr(ticket, "version", 0) or 0)
            live_status = str(getattr(ticket, "status", "") or "")
            details["ticket_version"] = live_version
            details["ticket_status"] = live_status
            expected = int(request.expected_version or 0)
            if not (
                live_version == expected
                or (live_status == "awaiting_approval" and live_version == expected + 1)
            ):
                reasons.append("ticket_version_mismatch")

    if operation_status is not None and operation_status != ApprovalStatus.PENDING:
        reasons.append("not_pending")
    scopes_set = set(scopes or ())
    if "ticket:approve" not in scopes_set and "chat:approve" not in scopes_set:
        reasons.append("denied_scope")
    return PreApprovalRecheck(ok=not reasons, fail_reasons=reasons, details=details)


async def recheck_pre_approval(
    *,
    runtime: Any,
    tenant_id: str,
    request: ReissueActionRequest,
    operation_status: ApprovalStatus | None = None,
    scopes: frozenset[str] | None = None,
) -> PreApprovalRecheck:
    """审批前再次执行六项重校验（读取 **LIVE 数据** 而非快照；任一不过 ``ok=False``，零副作用）。

    六项检查（与验收一一对应，每个失败项都是**互相区分**的错误码）：

        (a) account_active  ：runtime.vpn_adapter.get_account_status(user_id) 命中且 status==active；
        (b) asset_ownership ：runtime.assets.get(tenant_id, asset_id) 归属==user_id，未删除、未 retired；
        (c) config_version  ：runtime.vpn_adapter.get_client_config_version(user_id) 仍可读（版本非空）；
        (d) ticket_version  ：runtime.tickets.get(tenant_id, ticket_id).version 未变化（==expected；
                             若已推进到 AWAITING_APPROVAL 则允许 ==expected+1）；
        (e) not_pending     ：operation_status 仍为 PENDING（未被并发推进/被打断，否则审批是重放）；
        (f) approve_scope   ：scopes 含 ticket:approve 或 chat:approve（纵深；API/approve 已校验）。

    本函数**不做任何写入/审批推进**：只读 LIVE 数据并返回结构化判定，全部失败负责在
    ``approve_reissue`` 里拒绝并审计 ``vpn_reissue_precheck_failed``。
    """
    if request.action == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG:
        return await _recheck_tenant_redeploy(
            runtime=runtime,
            tenant_id=tenant_id,
            request=request,
            operation_status=operation_status,
            scopes=scopes,
        )

    fail_reasons: list[str] = []
    details: dict[str, Any] = {}

    adapter = getattr(runtime, "vpn_adapter", None)
    assets = getattr(runtime, "assets", None)
    tickets = getattr(runtime, "tickets", None)

    # (a) 账号仍然有效（LIVE）
    account_status: str | None = None
    if adapter is None or not hasattr(adapter, "get_account_status"):
        fail_reasons.append("adapter_missing")
    else:
        try:
            acct = await adapter.get_account_status(request.user_id)
        except Exception as exc:  # noqa: BLE001  查询异常一律作为拒绝原因，不阻断校验
            logger.warning("重校验：账号查询异常 user=%s: %s", request.user_id, type(exc).__name__)
            acct = None
        account_status = str(acct.get("status") or "") if isinstance(acct, dict) else ""
        details["account_status"] = account_status
        if not (isinstance(acct, dict) and acct.get("found") and account_status == "active"):
            fail_reasons.append("account_not_active")

    # (b) 资产仍属于当前租户（未删除、未退休、归属一致）
    asset_owner: str | None = None
    asset_status: str | None = None
    if assets is None or not request.asset_id:
        fail_reasons.append("asset_missing_or_unconfigured")
    else:
        try:
            asset = await assets.get(tenant_id, request.asset_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "重校验：资产查询异常 asset=%s: %s", request.asset_id, type(exc).__name__
            )
            asset = None
        if asset is None:
            fail_reasons.append("asset_not_found")
        else:
            asset_owner = getattr(asset, "owner_user_id", None)
            asset_status = str(getattr(asset, "status", None) or "")
            is_deleted = bool(getattr(asset, "is_deleted", False))
            details["asset_owner_user_id"] = asset_owner
            details["asset_status"] = asset_status
            if is_deleted:
                fail_reasons.append("asset_deleted")
            elif asset_owner != request.user_id:
                fail_reasons.append("asset_owner_mismatch")
            elif asset_status == "retired":
                fail_reasons.append("asset_retired")

    # (c) 目标版本仍然存在（客户端配置版本仍可读）
    config_version: str | None = None
    if adapter is None or not hasattr(adapter, "get_client_config_version"):
        fail_reasons.append("config_version_unavailable")
    else:
        try:
            cfg = await adapter.get_client_config_version(request.user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "重校验：配置版本查询异常 user=%s: %s", request.user_id, type(exc).__name__
            )
            cfg = None
        config_version = str(cfg.get("version") or "") if isinstance(cfg, dict) else ""
        details["client_config_version"] = config_version
        if not (isinstance(cfg, dict) and cfg.get("found") and config_version):
            fail_reasons.append("config_version_unavailable")

    # (d) 工单版本未变化（LIVE）
    live_ticket_version: int | None = None
    live_ticket_status: str | None = None
    if tickets is None or not hasattr(tickets, "get"):
        fail_reasons.append("ticket_unavailable")
    else:
        try:
            ticket = await tickets.get(tenant_id, request.ticket_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "重校验：工单查询异常 ticket=%s: %s", request.ticket_id, type(exc).__name__
            )
            ticket = None
        if ticket is None:
            fail_reasons.append("ticket_not_found")
        else:
            live_ticket_version = int(getattr(ticket, "version", 0) or 0)
            live_ticket_status = str(getattr(ticket, "status", "") or "")
            details["ticket_version"] = live_ticket_version
            details["ticket_status"] = live_ticket_status
            expected = int(request.expected_version or 0)
            # start 阶段的 REQUEST_APPROVAL 迁移会把版本推进 +1（进入 AWAITING_APPROVAL），
            # 视为「预期进入审批态」；其余任何增量都视为并发修改（TOCTOU 守卫）。
            if not (
                live_ticket_version == expected
                or (
                    live_ticket_status == "awaiting_approval"
                    and live_ticket_version == expected + 1
                )
            ):
                fail_reasons.append("ticket_version_mismatch")

    # (e) 当前操作仍未执行（仍为 PENDING，否则审批是 no-op 重放）
    if operation_status is not None and operation_status != ApprovalStatus.PENDING:
        fail_reasons.append("not_pending")
        details["operation_status"] = operation_status.value

    # (f) 审批人具备 ticket:approve / chat:approve scope（纵深；外部已校验）
    scopes_set = set(scopes or ())
    if "ticket:approve" not in scopes_set and "chat:approve" not in scopes_set:
        fail_reasons.append("denied_scope")

    return PreApprovalRecheck(
        ok=not fail_reasons,
        fail_reasons=fail_reasons,
        details=details,
    )


# ===========================================================================
# 高风险场景强制人工（阶段五）
# ===========================================================================

# 高风险重发：仅当审批人具备该显式 scope 时才允许自动/常规审批通过；
# 否则一律拒绝并转人工（不强求引入新的审批通道，不破坏既有 approve 流程）。
HIGH_RISK_APPROVE_SCOPE = "vpn:high_risk_approve"
_HIGH_RISK_APPROVE_SCOPES: frozenset[str] = frozenset({HIGH_RISK_APPROVE_SCOPE})

# 高风险触发词 -> 归一化高风险原因（同时接受英文编码与中文业务标签）。
_HIGH_RISK_ALIASES: dict[str, str] = {
    "auth_failed": "high_risk_auth_failed",
    "authentication_failed": "high_risk_auth_failed",
    "multi_user_impact": "high_risk_multi_user_impact",
    "multi_user": "high_risk_multi_user_impact",
    "permission_change": "high_risk_permission_change",
    "permission_changed": "high_risk_permission_change",
    "权限变更": "high_risk_permission_change",
    "权限修改": "high_risk_permission_change",
    "production": "high_risk_production_env",
    "production_env": "high_risk_production_env",
    "生产环境": "high_risk_production_env",
    "company_wide": "high_risk_company_wide_impact",
    "company_wide_impact": "high_risk_company_wide_impact",
    "companywide": "high_risk_company_wide_impact",
    "全公司影响": "high_risk_company_wide_impact",
    "data_breach": "high_risk_data_breach",
    "breach": "high_risk_data_breach",
    "数据泄露": "high_risk_data_breach",
    "account_unlock": "high_risk_account_unlock",
    "unlock": "high_risk_account_unlock",
    "账号解锁": "high_risk_account_unlock",
    "gateway_config_modification": "high_risk_gateway_config_modification",
    "gateway_config_change": "high_risk_gateway_config_modification",
    "网关配置修改": "high_risk_gateway_config_modification",
}

# 高风险原因的输出顺序（确定性，供审计/展示与测试断言）。
_HIGH_RISK_ORDER: tuple[str, ...] = (
    "high_risk_auth_failed",
    "high_risk_multi_user_impact",
    "high_risk_permission_change",
    "high_risk_production_env",
    "high_risk_company_wide_impact",
    "high_risk_data_breach",
    "high_risk_account_unlock",
    "high_risk_gateway_config_modification",
)


def high_risk_reasons(
    *,
    request: ReissueActionRequest | None = None,
    fault: str | None = None,
    flags: list[str] | None = None,
    reason_codes: list[str] | None = None,
) -> list[str]:
    """确定性判定「高风险场景」（纯函数，无 IO）。

    高风险场景只能人工（requires_human=True / 拒绝自动审批，或要求显式
    ``vpn:high_risk_approve`` 审批 scope 越过 ``ticket:approve``）：

        - fault == "auth_failed" / "multi_user_impact"               -> 强制 requires_human；
        - reason_codes / flags 命中
          权限变更 / 生产环境 / 全公司影响 / 数据泄露 / 账号解锁 / 网关配置修改
          -> 阻断自动。

    输入来源：``request.reason_codes``（阶段三/四诊断结论映射的可选入参）、``fault``
    （诊断 fault，阶段四 DiagnosisConclusion 就绪后接入）、``flags``（scope/环境标记）。
    返回按固定优先级排序、去重的归一化高风险原因列表；空列表 = 非高风险。
    """
    tokens: list[str] = []

    if fault:
        tokens.append(str(fault))
    codes = (
        reason_codes
        if reason_codes is not None
        else (list(request.reason_codes) if request is not None else [])
    )
    tokens.extend(str(c) for c in codes)
    tokens.extend(str(f) for f in (flags or []))

    seen: set[str] = set()
    for token in tokens:
        key = token.strip().lower()
        canonical = _HIGH_RISK_ALIASES.get(token.strip()) or _HIGH_RISK_ALIASES.get(key)
        if canonical and canonical not in seen:
            seen.add(canonical)

    return [r for r in _HIGH_RISK_ORDER if r in seen]


# ===========================================================================
# 幂等/审批状态登记表
# ===========================================================================


@dataclass
class ReissueRegistry:
    """审批式执行动作的幂等/状态登记表（内存实现）。

    生产环境应把该表映射到 workflow_operation（operation_id 幂等 + committed 终态）
    与 audit 事件（审计留痕）；此处提供支持单元测试的内存版，保证「同键重复
    不重复执行」「未 APPROVED 不执行」两个硬验收在纯函数层可验证。

    阶段五：增补 request 快照与 scan_reconcilable()，供补偿对账扫描
    execution_unknown / reconciliation_required 键而无需外部入参（供 worker 调用）。
    """

    _status: dict[str, ApprovalStatus] = field(default_factory=dict)
    _result: dict[str, ReissueExecutionResult] = field(default_factory=dict)
    _request: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_status(self, idempotency_key: str) -> ApprovalStatus | None:
        return self._status.get(idempotency_key)

    def set_status(self, idempotency_key: str, status: ApprovalStatus) -> None:
        self._status[idempotency_key] = status

    def get_result(self, idempotency_key: str) -> ReissueExecutionResult | None:
        return self._result.get(idempotency_key)

    def set_result(self, idempotency_key: str, result: ReissueExecutionResult) -> None:
        self._result[idempotency_key] = result
        self._status[idempotency_key] = result.status

    def set_request(self, idempotency_key: str, request: ReissueActionRequest) -> None:
        self._request[idempotency_key] = request.model_dump(mode="json")

    def get_request(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._request.get(idempotency_key)

    def scan_reconcilable(self) -> list[str]:
        """返回需要补偿对账的幂等键（execution_unknown / reconciliation_required）。"""
        return [
            key
            for key, status in self._status.items()
            if status in (ApprovalStatus.EXECUTION_UNKNOWN, ApprovalStatus.RECONCILIATION_REQUIRED)
        ]


# 模块级默认登记表（测试隔离可注入独立实例）
_DEFAULT_REGISTRY = ReissueRegistry()

# 公共别名：供 VpnReissueService 与 reissue_vpn_config 工具共享同一审批状态登记表，
# 保证「工具」与「服务」两个执行入口看到同一个 APPROVED / 终态，消除双入口不一致。
DEFAULT_REISSUE_REGISTRY = _DEFAULT_REGISTRY


# ===========================================================================
# 缺省存储（共享单例）与操作/请求重建辅助
# ===========================================================================


# 共享缺省 ReissueStore（内存实现，包裹模块级默认登记表）。懒加载避免与
# reissue_store 的循环导入；单例保证「工具（reissue_vpn_config）」与「服务
# （VpnReissueService）」两个执行入口共享同一存储，消除双入口状态不一致。
_default_store: Any = None


def get_default_reissue_store() -> Any:
    """返回共享的缺省 ReissueStore（内存实现，包裹模块级默认登记表 ReissueRegistry）。

    单例：只创建一次；所有缺省 `store` 参数与未显式传 registry 的 VpnReissueService
    都复用同一实例，从而「工具」与「服务」读到同一份 APPROVED / 终态。
    """
    global _default_store
    if _default_store is None:
        from .reissue_store import InMemoryReissueStore

        _default_store = InMemoryReissueStore(registry=_DEFAULT_REGISTRY)
    return _default_store


def _operation_to_request(op: Any) -> ReissueActionRequest | None:
    """从 ReissueOperation.request_snapshot 重建 ReissueActionRequest。

    请求内容（目标用户/资产/版本）一律取自存储快照，不信任审批人/调用方入参。
    """
    if op is None or not op.request_snapshot:
        return None
    return ReissueActionRequest(**op.request_snapshot)


def _operation_result(op: Any) -> ReissueExecutionResult | None:
    """从 ReissueOperation 重建既有执行结果（仅 delivered/confirmed 终态，幂等用）。"""
    if op is None:
        return None
    status = op.status
    if status not in (ApprovalStatus.DELIVERED, ApprovalStatus.CONFIRMED):
        return None
    return ReissueExecutionResult(
        ok=True,
        action=ReissueAction((op.request_snapshot or {}).get("action", ReissueAction.REISSUE_VPN_CONFIG)),
        status=status,
        idempotency_key=op.idempotency_key,
        delivered=True,
        confirmed=(status == ApprovalStatus.CONFIRMED),
        error_code=op.error_code,
        detail=dict(op.external_result or {}),
    )


def _transition_error(
    transition: Any, *, idempotency_key: str, request: ReissueActionRequest
) -> ReissueExecutionResult:
    """把 store.set_status 的非成功结果映射为结构化错误（供 API 层映射 409/503）。"""
    from .reissue_store import ReissueTransitionResult

    if transition == ReissueTransitionResult.DB_ERROR:
        code, reason = "db_error", "存储故障，审批操作不可用"
    elif transition == ReissueTransitionResult.VERSION_CONFLICT:
        code, reason = "version_conflict", "操作已被并发处理"
    elif transition == ReissueTransitionResult.ALREADY_TARGET_STATE:
        code, reason = "already_target_state", "已是目标状态（幂等成功）"
    else:
        code, reason = "illegal_transition", "非法状态机跳转，已拒绝"
    return ReissueExecutionResult(
        ok=False,
        action=request.action,
        status=ApprovalStatus.PENDING,
        idempotency_key=idempotency_key,
        reason=reason,
        error_code=code,
        detail={"request": _request_id(request)},
    )


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
        "target_snapshot": request.target_snapshot,
        "diff_hash": request.diff_hash,
        "desired_state_hash": request.desired_state_hash,
    }


# ===========================================================================
# 发起审批 → 审批（批准/拒绝/取消）→ 执行（受控入口）
# ===========================================================================


async def start_reissue_approval(
    *,
    runtime: Any,
    run_context: Any,
    tenant_id: str,
    user_id: str | None,
    ticket_id: str,
    client_version: str = "tenant-current",
    action: ReissueAction = ReissueAction.REISSUE_VPN_CONFIG,
    asset_id: str | None = None,
    idempotency_key: str | None = None,
    expected_version: int = 0,
    reason_codes: list[str] | None = None,
    registry: ReissueRegistry | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """发起一次 reissue_vpn_config 审批。

    流程：生成幂等键 → 幂等（同键已交付/确认则直接返回既有结果）→ 前置校验 →
        通过则经 store.create_operation 登记 PENDING 并审计 vpn_reissue_started；
        不过则登记 FAILED 并审计 vpn_reissue_preflight_failed，返回拒绝原因。

    返回：{"request", "preflight", "status", "idempotency_key"}（供审批卡/审计）。
    """
    store = store or get_default_reissue_store()
    key = idempotency_key or (
        build_tenant_redeploy_idempotency_key(tenant_id=tenant_id, ticket_id=ticket_id)
        if action == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG
        else build_reissue_idempotency_key(
            tenant_id=tenant_id,
            user_id=user_id or "",
            asset_id=asset_id,
            ticket_id=ticket_id,
            client_version=client_version,
        )
    )

    request = ReissueActionRequest(
        action=action,
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
    existing_op = await store.get_by_idempotency_key(tenant_id=tenant_id, idempotency_key=key)
    existing = _operation_result(existing_op)
    if existing is not None:
        existing_snapshot = dict(existing_op.request_snapshot or {})
        return {
            "request": existing_snapshot,
            "preflight": ReissuePreflightResult(ok=True).model_dump(mode="json"),
            "status": existing.status.value,
            "idempotency_key": key,
            "existing_result": existing.model_dump(mode="json"),
        }

    preflight = await preflight_reissue(
        runtime=runtime,
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        action=action,
    )
    request = request.model_copy(
        update={
            "target_snapshot": preflight.target_snapshot,
            "diff_snapshot": preflight.diff_snapshot,
            "diff_hash": preflight.diff_hash,
            "desired_state_hash": preflight.desired_state_hash,
        }
    )
    snapshot = request.model_dump(mode="json")

    # preview 无差异是安全 no-op，不进入审批，不允许产生 install。
    if (
        action == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG
        and preflight.ok
        and preflight.preview_status == "noop"
    ):
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_noop",
            status="noop",
            payload={**_request_id(request), "reason": "preview_no_diff"},
        )
        return {
            "request": snapshot,
            "preflight": preflight.model_dump(mode="json"),
            "status": "noop",
            "idempotency_key": key,
            "approval_required": False,
        }
    if not preflight.ok:
        await store.create_operation(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            operation_id=key,
            idempotency_key=key,
            request_snapshot=snapshot,
            status=ApprovalStatus.FAILED,
            expected_version=expected_version,
        )
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
            "request": snapshot,
            "preflight": preflight.model_dump(mode="json"),
            "status": ApprovalStatus.FAILED.value,
            "idempotency_key": key,
            "rejected": True,
            "fail_reasons": preflight.fail_reasons,
        }

    await store.create_operation(
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        operation_id=key,
        idempotency_key=key,
        request_snapshot=snapshot,
        status=ApprovalStatus.PENDING,
        expected_version=expected_version,
    )
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
        "request": snapshot,
        "preflight": preflight.model_dump(mode="json"),
        "status": ApprovalStatus.PENDING.value,
        "idempotency_key": key,
        "approval_required": True,
    }


async def approve_reissue(
    *,
    tenant_id: str,
    operation_id: str,
    runtime: Any,
    run_context: Any,
    approver_user_id: str,
    registry: ReissueRegistry | None = None,
    store: Any | None = None,
) -> ReissueExecutionResult:
    """审批人批准：仅把操作从 PENDING 置为 APPROVED，**不自动执行**。

    请求内容一律从 ``store.get_operation`` 按 ``operation_id`` 读取并重建
    （request_snapshot），不信任调用方/审批人入参（防篡改）；真实的受控下发由
    ``execute_approved_reissue`` 单独触发（服务层 approve 在成功后随之执行）。

    映射 ``store.set_status`` 结果：SUCCESS / ALREADY_TARGET_STATE 视为幂等成功；
    ILLEGAL_TRANSITION / VERSION_CONFLICT / DB_ERROR 映射为结构化错误（供 API 层 409/503）。
    """
    from .reissue_store import ReissueTransitionResult

    store = store or get_default_reissue_store()
    op = await store.get_operation(tenant_id=tenant_id, operation_id=operation_id)
    if op is None:
        return ReissueExecutionResult(
            ok=False,
            status=ApprovalStatus.PENDING,
            idempotency_key=operation_id,
            reason="审批操作不存在",
            error_code="not_found",
        )
    key = op.idempotency_key
    request = _operation_to_request(op)
    if request is None:
        return ReissueExecutionResult(
            ok=False,
            status=ApprovalStatus.PENDING,
            idempotency_key=key,
            reason="审批请求快照缺失",
            error_code="missing_request_snapshot",
        )

    # 幂等：同键已交付/确认 → 直接返回既有结果，不重复执行（重复审批不重复执行）。
    existing = _operation_result(op)
    if existing is not None:
        return existing

    # 风险 F：审批人 scope 校验（外部 /chat/resume 已强制 chat:approve；模块层纵深）。
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
            action=request.action,
            status=ApprovalStatus.PENDING,
            idempotency_key=key,
            reason="缺少审批权限",
            error_code="denied_scope",
        )

    approved = request.model_copy(update={"approver_user_id": approver_user_id})

    # 阶段五：审批前重校验（读取 LIVE 数据而非快照；任一失败零副作用，绝不推进审批）。
    recheck = await recheck_pre_approval(
        runtime=runtime,
        tenant_id=tenant_id,
        request=request,
        operation_status=op.status,
        scopes=frozenset(scopes),
    )
    if not recheck.ok:
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_precheck_failed",
            status="failed",
            payload={**_request_id(request), "fail_reasons": recheck.fail_reasons},
        )
        return ReissueExecutionResult(
            ok=False,
            action=request.action,
            status=ApprovalStatus.PENDING,
            idempotency_key=key,
            reason="审批前重校验未通过",
            error_code="precheck_failed",
            detail={"fail_reasons": recheck.fail_reasons},
        )

    # 阶段五：高风险场景强制人工——无显式人工审批 scope（vpn:high_risk_approve）则拒绝自动审批。
    high_risk = high_risk_reasons(request=request, flags=list(scopes))
    if high_risk and not (scopes & _HIGH_RISK_APPROVE_SCOPES):
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_high_risk_denied",
            status="denied",
            payload={**_request_id(request), "high_risk_reasons": high_risk},
        )
        return ReissueExecutionResult(
            ok=False,
            action=request.action,
            status=ApprovalStatus.PENDING,
            idempotency_key=key,
            reason="高风险场景需显式人工审批 scope，拒绝自动审批",
            error_code="high_risk_requires_human",
            detail={"high_risk_reasons": high_risk},
        )

    transition = await store.set_status(
        tenant_id=tenant_id,
        operation_id=operation_id,
        from_status=ApprovalStatus.PENDING,
        to_status=ApprovalStatus.APPROVED,
        approver_user_id=approver_user_id,
    )
    if transition not in (
        ReissueTransitionResult.SUCCESS,
        ReissueTransitionResult.ALREADY_TARGET_STATE,
    ):
        return _transition_error(transition, idempotency_key=key, request=approved)

    await _audit(
        runtime,
        run_context,
        "vpn_reissue_approved",
        status="approved",
        payload={**_request_id(approved)},
    )
    return ReissueExecutionResult(
        ok=True,
        action=approved.action,
        status=ApprovalStatus.APPROVED,
        idempotency_key=key,
        error_code="approved",
    )


async def reject_reissue(
    *,
    request: ReissueActionRequest,
    runtime: Any,
    run_context: Any,
    approver_user_id: str | None = None,
    reason: str = "审批拒绝",
    registry: ReissueRegistry | None = None,
    store: Any | None = None,
) -> ReissueExecutionResult:
    """审批人拒绝：经 store.set_status 置 REJECTED（人工接管），审计 vpn_reissue_rejected。"""
    store = store or get_default_reissue_store()
    op = await store.get_operation(
        tenant_id=request.tenant_id, operation_id=request.idempotency_key
    )
    from_status = op.status if op is not None else ApprovalStatus.PENDING
    await store.set_status(
        tenant_id=request.tenant_id,
        operation_id=request.idempotency_key,
        from_status=from_status,
        to_status=ApprovalStatus.REJECTED,
        approver_user_id=approver_user_id,
    )
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
        action=request.action,
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
    store: Any | None = None,
) -> ReissueExecutionResult:
    """取消：经 store.set_status 置 CANCELLED（人工接管），审计 vpn_reissue_cancelled（带审批人）。"""
    store = store or get_default_reissue_store()
    op = await store.get_operation(
        tenant_id=request.tenant_id, operation_id=request.idempotency_key
    )
    from_status = op.status if op is not None else ApprovalStatus.PENDING
    await store.set_status(
        tenant_id=request.tenant_id,
        operation_id=request.idempotency_key,
        from_status=from_status,
        to_status=ApprovalStatus.CANCELLED,
        approver_user_id=approver_user_id,
    )
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
        action=request.action,
        status=ApprovalStatus.CANCELLED,
        idempotency_key=request.idempotency_key,
        reason=reason,
        error_code="cancelled",
    )


# ===========================================================================
# 异常分类（阶段五）：把「外部结果未知」与「明确失败」区分开，不吞掉异常。
# ===========================================================================

# 外部结果「歧义/未知」错误码集合（超时/无明确应答等）——这类不应误判为失败，而应收敛对账。
_AMBIGUOUS_ERROR_CODES: frozenset[str] = frozenset(
    {
        "timeout",
        "external_timeout",
        "execution_timeout",
        "external_result_unknown",
        "unknown",
        "vendor_timeout",
        "timeout_unknown",
        # install 请求可能已被 FMG 接收，但响应没有返回 task id；禁止自动重发。
        "submission_unknown",
    }
)

# 需要补偿对账的状态。
_RECONCILABLE_STATES = (
    ApprovalStatus.EXECUTION_UNKNOWN,
    ApprovalStatus.RECONCILIATION_REQUIRED,
)


def _is_ambiguous_error_code(error_code: Any) -> bool:
    return isinstance(error_code, str) and error_code.lower() in _AMBIGUOUS_ERROR_CODES


def _is_timeout_exception(exc: BaseException) -> bool:
    """无明确外部响应的传输异常 → 结果未知，不能安全重发写操作。"""
    if isinstance(exc, TimeoutError):
        return True
    # 写请求的连接中断同样可能发生在请求已被 FortiManager 接收之后。
    # 在没有厂商请求级幂等键时，保守地交给补偿对账，避免重复 install。
    if isinstance(exc, ConnectionError):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or name in {
        "asyncio.timeout_error",
        "timeouterror",
        "transporterror",
        "networkerror",
        "connectionerror",
    }


def classify_reissue_outcome(outcome: dict[str, Any]) -> tuple[ApprovalStatus, str | None]:
    """把外部 reissue 结果归类为终态（区分异常类型，不吞掉异常）。

    规则：
        - delivered/confirmed            → CONFIRMED / DELIVERED；
        - error_code 为歧义/未知类       → EXECUTION_UNKNOWN（需对账，不误判为失败）；
        - 其余（业务拒绝/权限失败/版本冲突/外部明确失败） → FAILED + error_code。
    返回 (status, error_code)。
    """
    delivered = bool(outcome.get("delivered", False))
    confirmed = bool(outcome.get("confirmed", delivered))
    if delivered or confirmed:
        return (ApprovalStatus.CONFIRMED if confirmed else ApprovalStatus.DELIVERED), None
    # 控制面 install 是异步 task：worker 轮询到 running 不能提前记为失败，只能继续对账。
    if outcome.get("status") in {"submitted", "pending", "running"}:
        return ApprovalStatus.EXECUTION_UNKNOWN, "vendor_task_pending"
    error_code = outcome.get("error_code")
    if _is_ambiguous_error_code(error_code):
        return ApprovalStatus.EXECUTION_UNKNOWN, str(error_code)
    code = str(error_code) if error_code else "reissue_failed"
    return ApprovalStatus.FAILED, code


async def execute_approved_reissue(
    *,
    request: ReissueActionRequest,
    runtime: Any,
    run_context: Any,
    registry: ReissueRegistry | None = None,
    store: Any | None = None,
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
    from .reissue_store import ReissueTransitionResult

    store = store or get_default_reissue_store()
    key = request.idempotency_key
    base_payload = _request_id(request)

    op = await store.get_operation(tenant_id=request.tenant_id, operation_id=key)
    # 幂等：同键已交付/确认 → 直接返回既有结果。
    existing = _operation_result(op)
    if existing is not None:
        return existing

    status = op.status if op is not None else None
    if status != ApprovalStatus.APPROVED:
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_denied",
            status="denied",
            payload={
                **base_payload,
                "reason": "not_approved",
                "current_status": status.value if status else None,
            },
        )
        return ReissueExecutionResult(
            ok=False,
            action=request.action,
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
        action=request.action,
    )
    if not preflight.ok:
        await store.set_status(
            tenant_id=request.tenant_id,
            operation_id=key,
            from_status=ApprovalStatus.APPROVED,
            to_status=ApprovalStatus.FAILED,
            error_code="preflight_failed",
        )
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_preflight_failed",
            status="failed",
            payload={**base_payload, "fail_reasons": preflight.fail_reasons},
        )
        return ReissueExecutionResult(
            ok=False,
            action=request.action,
            status=ApprovalStatus.FAILED,
            idempotency_key=key,
            reason="前置校验未通过",
            error_code="preflight_failed",
            detail={"fail_reasons": preflight.fail_reasons},
        )

    # 进入 EXECUTING（条件 CAS：APPROVED->EXECUTING）。
    transition = await store.set_status(
        tenant_id=request.tenant_id,
        operation_id=key,
        from_status=ApprovalStatus.APPROVED,
        to_status=ApprovalStatus.EXECUTING,
    )
    if transition not in (
        ReissueTransitionResult.SUCCESS,
        ReissueTransitionResult.ALREADY_TARGET_STATE,
    ):
        return _transition_error(transition, idempotency_key=key, request=request)

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
        runner_kwargs: dict[str, Any] = {
            "tenant_id": request.tenant_id,
            "user_id": request.user_id,
            "idempotency_key": key,
            "target_version": request.client_version,
        }
        if request.action == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG:
            runner_kwargs.update(
                {
                    "action": request.action.value,
                    "approved_diff_hash": request.diff_hash,
                }
            )
        try:
            outcome = await runner(runtime, **runner_kwargs)
        except TypeError:
            # 兼容旧注入执行器；租户级生产网关必须支持新参数，不走此回退。
            if request.action == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG:
                raise
            runner_kwargs.pop("action", None)
            runner_kwargs.pop("approved_diff_hash", None)
            outcome = await runner(runtime, **runner_kwargs)
    except Exception as exc:  # noqa: BLE001
        if _is_timeout_exception(exc):
            # 外部超时：结果未知（可能已下发），不误判为失败 → 转 execution_unknown，交对账。
            logger.warning("reissue 执行结果未知 key=%s: %s", key, type(exc).__name__)
            await store.set_status(
                tenant_id=request.tenant_id,
                operation_id=key,
                from_status=ApprovalStatus.EXECUTING,
                to_status=ApprovalStatus.EXECUTION_UNKNOWN,
            )
            await _audit(
                runtime,
                run_context,
                "vpn_reissue_execution_unknown",
                status="execution_unknown",
                payload={
                    **base_payload,
                    "error_type": type(exc).__name__,
                    "reason": "external_timeout_unknown",
                },
            )
            return ReissueExecutionResult(
                ok=False,
                action=request.action,
                status=ApprovalStatus.EXECUTION_UNKNOWN,
                idempotency_key=key,
                reason="外部结果未知（可能已下发），待补偿对账",
                error_code="execution_unknown",
                detail={"error_type": type(exc).__name__},
            )
        logger.warning("reissue 执行失败 key=%s: %s", key, type(exc).__name__)
        await store.set_status(
            tenant_id=request.tenant_id,
            operation_id=key,
            from_status=ApprovalStatus.EXECUTING,
            to_status=ApprovalStatus.FAILED,
            error_code="execute_failed",
        )
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_failed",
            status="failed",
            payload={**base_payload, "error_type": type(exc).__name__, "reason": "execute_failed"},
        )
        return ReissueExecutionResult(
            ok=False,
            action=request.action,
            status=ApprovalStatus.FAILED,
            idempotency_key=key,
            reason="执行失败，可重试或人工接管",
            error_code="execute_failed",
            detail={"error_type": type(exc).__name__},
        )

    if not isinstance(outcome, dict):
        outcome = {}
    outcome_status, error_code = classify_reissue_outcome(outcome)
    delivered = bool(outcome.get("delivered", False))
    confirmed = bool(outcome.get("confirmed", delivered))
    if outcome_status == ApprovalStatus.EXECUTION_UNKNOWN:
        # 外部结果未知（歧义错误码）→ 转 execution_unknown，交对账。
        await store.set_status(
            tenant_id=request.tenant_id,
            operation_id=key,
            from_status=ApprovalStatus.EXECUTING,
            to_status=ApprovalStatus.EXECUTION_UNKNOWN,
            error_code=str(error_code) if error_code else None,
            external_result=outcome,
        )
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_execution_unknown",
            status="execution_unknown",
            payload={
                **base_payload,
                "result": outcome,
                "reason": outcome.get("reason") or "external_result_unknown",
            },
        )
        return ReissueExecutionResult(
            ok=False,
            action=request.action,
            status=ApprovalStatus.EXECUTION_UNKNOWN,
            idempotency_key=key,
            reason=outcome.get("reason") or "外部结果未知，待补偿对账",
            error_code=str(error_code) if error_code else "execution_unknown",
            detail=outcome,
        )
    if outcome_status == ApprovalStatus.FAILED:
        # 明确失败（业务拒绝/权限失败/版本冲突/外部失败）→ FAILED。
        await store.set_status(
            tenant_id=request.tenant_id,
            operation_id=key,
            from_status=ApprovalStatus.EXECUTING,
            to_status=ApprovalStatus.FAILED,
            error_code=str(error_code) if error_code else "reissue_failed",
        )
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_failed",
            status="failed",
            payload={
                **base_payload,
                "result": outcome,
                "reason": outcome.get("reason") or "reissue_failed",
                "error_code": error_code,
            },
        )
        return ReissueExecutionResult(
            ok=False,
            action=request.action,
            status=ApprovalStatus.FAILED,
            idempotency_key=key,
            reason=outcome.get("reason") or "重新下发失败",
            error_code=str(error_code) if error_code else "reissue_failed",
            detail=outcome,
        )

    final_status = outcome_status  # CONFIRMED 或 DELIVERED
    result = ReissueExecutionResult(
        ok=True,
        action=request.action,
        status=final_status,
        idempotency_key=key,
        delivered=delivered,
        confirmed=confirmed,
        detail=outcome,
    )
    await store.set_result(tenant_id=request.tenant_id, operation_id=key, result=result)
    await _audit(
        runtime,
        run_context,
        "vpn_reissue_executed",
        status="completed",
        payload={**base_payload, "result": result.model_dump(mode="json")},
    )
    return result


# 缺省执行器：优先独立控制面网关；仅 mock/sandbox 保留原 adapter 兼容路径。
async def _default_execute_reissue(
    runtime: Any,
    *,
    tenant_id: str,
    user_id: str | None,
    idempotency_key: str,
    target_version: str,
    action: str = "reissue_vpn_config",
    approved_diff_hash: str | None = None,
) -> dict[str, Any]:
    command_gateway = getattr(runtime, "vpn_command_gateway", None)
    if command_gateway is not None:
        if action == ReissueAction.REDEPLOY_TENANT_VPN_CONFIG.value:
            return await command_gateway.redeploy_tenant_vpn_config(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                action=action,
                user_id=user_id,
                target_version=target_version,
                approved_diff_hash=approved_diff_hash,
                enforce_preview=True,
            )
        return await command_gateway.reissue_config(
            tenant_id=tenant_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
            target_version=target_version,
        )
    adapter = getattr(runtime, "vpn_adapter", None)
    if adapter is None or not hasattr(adapter, "reissue_config"):
        raise RuntimeError("vpn_adapter 未配置 reissue_config，无法执行下发")
    if not user_id:
        raise RuntimeError("租户级下发未配置 FortiManager command gateway")
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
    store: Any | None = None,
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
        action=request.action,
        registry=registry,
        store=store,
    )
    return {"dispatched": True, **outcome}
