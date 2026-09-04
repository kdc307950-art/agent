"""重新下发 VPN 配置文件（reissue_vpn_config）的运行时编排层。

模块归属：backend/vpn。位置：backend/vpn/approval.py（纯逻辑核心）之上、API/图之下。

职责（对齐 docs/product/vpn-diagnosis-agent-contract.md 的「只诊断、不自动执行」边界 +
用户对「审批式执行动作」的六项硬验收，并落实 reviewer 的 C1–C5 最低门槛）：
    - 复用 approval.py 的纯逻辑（preflight / 幂等键 / 审批状态 / 审计 / execute_approved_reissue），
      本服务只负责把这些逻辑接到「工单状态机 + workflow_operation 持久化终态」：
    - 发起审批：登记 PENDING + 落一条 ticket_workflow_runs(operation_id=幂等键, status=started)
      + 工单迁移到 AWAITING_APPROVAL（domain: IN_PROGRESS + REQUEST_APPROVAL）。
    - 审批通过：经 approve_reissue → execute_approved_reissue（受限执行入口），
      成功 → workflow_operation 置 committed 终态（C1）+ 工单回 IN_PROGRESS；
      失败 → mark_workflow_operation_failed(error_code)（C1）+ 工单回到可接管仲裁态（C3）。
    - 拒绝/取消：置 REJECTED/CANCELLED + 工单回到可接管仲裁态（C3）。
    - 每次关键点写 audit.record_event，payload 关联 tenant/user/asset/ticket/approver/
      idempotency_key/工具名(reissue_vpn_config)/result（C2 + 用户验收 3）。

红线：本服务只处理 reissue_vpn_config（标准客户端配置交付）。modify_vpn_config（改服务器/
网关/防火墙）仍是 FORBIDDEN 红线，绝不进入 action 白名单；未 APPROVED 的执行入口
execute_approved_reissue 一律拒绝（not_approved），零副作用（用户红线）。

持久化说明：approval.py 的 ReissueRegistry 提供审批状态机（内存版，可单测）；本服务在其上
用 runtime.tickets 的 workflow_operation 原语落「结构化终态 + 幂等锚点」（满足 C1/C2/C5）。
真实调用 VPN 后台的重下发由 runtime.vpn_adapter.reissue_config 经 execute_approved_reissue
执行，且必须带幂等键，保证「审批了但执行前超时/死机」重放链也只执行一次（C5）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.my_agent.helpdesk import ActorType, TicketAction, TicketCommand, transition_ticket

from .approval import (
    DEFAULT_REISSUE_REGISTRY,
    ApprovalStatus,
    ReissueActionRequest,
    ReissueExecutionResult,
    ReissueRegistry,
    approve_reissue,
    build_reissue_idempotency_key,
    cancel_reissue,
    execute_approved_reissue,
    reject_reissue,
    start_reissue_approval,
)

logger = logging.getLogger("langgraph.vpn")

# 该动作在 workflow_operation 里的 command_type（对齐 audit / 幂等锚点）。
REISSUE_COMMAND_TYPE = "reissue_vpn_config"


class VpnReissueService:
    """审批式执行的运行时编排服务。

    不持有 runtime 引用（避免装配期循环依赖）：所有方法在执行时显式接收 runtime/run_context。
    单测可注入独立 ReissueRegistry 与 fake runtime（audit/tickets/vpn_adapter）。
    """

    def __init__(self, *, registry: ReissueRegistry | None = None) -> None:
        # #2：默认共享模块级登记表（与 reissue_vpn_config 工具同一实例），消除双入口不一致。
        self.registry = registry or DEFAULT_REISSUE_REGISTRY

    # ---- 公有编排入口 ----

    async def start(
        self,
        *,
        request: ReissueActionRequest,
        runtime: Any,
        run_context: Any,
    ) -> dict[str, Any]:
        """发起一次重新下发配置的审批。

        流程（复用 approval.start_reissue_approval）：
            前置校验 → 通过则登记 PENDING 并审计 vpn_reissue_started → 落 workflow_operation
            started + 工单迁移到 AWAITING_APPROVAL；前置校验不过则登记 FAILED 并审计
            vpn_reissue_preflight_failed（返回拒绝原因）。
        """
        tenant_id = request.tenant_id
        ticket_id = request.ticket_id
        outcome = await start_reissue_approval(
            runtime=runtime,
            run_context=run_context,
            tenant_id=tenant_id,
            user_id=request.user_id,
            asset_id=request.asset_id,
            ticket_id=ticket_id,
            client_version=request.client_version,
            idempotency_key=request.idempotency_key,
            expected_version=request.expected_version,
            reason_codes=request.reason_codes,
            registry=self.registry,
        )
        status = outcome.get("status")
        if status == ApprovalStatus.PENDING.value:
            await self._record_operation_started(runtime, run_context, request)
            await self._transition_ticket(
                runtime, run_context, request, TicketAction.REQUEST_APPROVAL
            )
        elif status == ApprovalStatus.FAILED.value:
            await self._mark_operation_failed(
                runtime,
                run_context,
                request,
                "preflight_failed",
                fail_reasons=outcome.get("fail_reasons") or [],
            )
        return outcome

    async def approve(
        self,
        *,
        request: ReissueActionRequest,
        approver_user_id: str,
        runtime: Any,
        run_context: Any,
    ) -> ReissueExecutionResult:
        """审批人批准 → 前置校验 → 受控执行；落 workflow_operation 终态 + 工单状态联动。

        幂等：同幂等键已交付/确认直接返回既有结果，不重复执行（重复审批/重复回调不重复执行）。
        """
        result = await approve_reissue(
            request=request,
            runtime=runtime,
            run_context=run_context,
            approver_user_id=approver_user_id,
            registry=self.registry,
        )
        await self._post_execute(runtime, run_context, request, result)
        return result

    async def reject(
        self,
        *,
        request: ReissueActionRequest,
        runtime: Any,
        run_context: Any,
        approver_user_id: str | None = None,
        reason: str = "审批拒绝",
    ) -> ReissueExecutionResult:
        """审批人拒绝：置 REJECTED（人工接管）+ 工单回可接管仲裁态。"""
        result = await reject_reissue(
            request=request,
            runtime=runtime,
            run_context=run_context,
            approver_user_id=approver_user_id,
            reason=reason,
            registry=self.registry,
        )
        await self._mark_operation_failed(runtime, run_context, request, "rejected")
        return result

    async def cancel(
        self,
        *,
        request: ReissueActionRequest,
        runtime: Any,
        run_context: Any,
        reason: str = "操作取消",
    ) -> ReissueExecutionResult:
        """取消：置 CANCELLED（人工接管）+ 工单回可接管仲裁态。"""
        result = await cancel_reissue(
            request=request,
            runtime=runtime,
            run_context=run_context,
            reason=reason,
            registry=self.registry,
        )
        await self._mark_operation_failed(runtime, run_context, request, "cancelled")
        return result

    async def get_result(self, *, idempotency_key: str) -> ReissueExecutionResult | None:
        return self.registry.get_result(idempotency_key)

    # ---- 受控执行（供工具 / 图节点直接调用，审批由 registry.APPROVED 强制） ----

    async def execute(
        self,
        *,
        request: ReissueActionRequest,
        runtime: Any,
        run_context: Any,
    ) -> ReissueExecutionResult:
        """受控执行入口：仅当已 APPROVED 才真实下发。落终态 + 工单联动。"""
        result = await execute_approved_reissue(
            request=request,
            runtime=runtime,
            run_context=run_context,
            registry=self.registry,
        )
        await self._post_execute(runtime, run_context, request, result)
        return result

    # ---- 内部：workflow_operation 终态 + 工单状态联动 ----

    async def _record_operation_started(
        self, runtime: Any, run_context: Any, request: ReissueActionRequest
    ) -> None:
        tickets = getattr(runtime, "tickets", None)
        if tickets is None or not hasattr(tickets, "start_workflow_operation"):
            return
        try:
            await tickets.start_workflow_operation(
                tenant_id=request.tenant_id,
                ticket_id=request.ticket_id,
                operation_id=request.idempotency_key,
                command_type=REISSUE_COMMAND_TYPE,
                expected_version=request.expected_version,
                checkpoint_thread_id=f"vpn-reissue:{request.idempotency_key}",
            )
        except Exception as exc:  # noqa: BLE001  不阻断主流程
            logger.warning("reissue 登记 workflow_operation 失败 key=%s: %s", request.idempotency_key, type(exc).__name__)

    async def _mark_operation_failed(
        self,
        runtime: Any,
        run_context: Any,
        request: ReissueActionRequest,
        error_code: str,
        *,
        fail_reasons: list[str] | None = None,
    ) -> None:
        tickets = getattr(runtime, "tickets", None)
        if tickets is None or not hasattr(tickets, "mark_workflow_operation_failed"):
            return
        try:
            await tickets.mark_workflow_operation_failed(
                tenant_id=request.tenant_id,
                ticket_id=request.ticket_id,
                operation_id=request.idempotency_key,
                error_code=error_code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reissue 标记 workflow_operation 失败 key=%s: %s", request.idempotency_key, type(exc).__name__)

    async def _mark_operation_committed(
        self,
        runtime: Any,
        request: ReissueActionRequest,
        result: ReissueExecutionResult,
    ) -> None:
        tickets = getattr(runtime, "tickets", None)
        if tickets is None or not hasattr(tickets, "mark_workflow_operation_committed"):
            return
        try:
            await tickets.mark_workflow_operation_committed(
                tenant_id=request.tenant_id,
                ticket_id=request.ticket_id,
                operation_id=request.idempotency_key,
                result_hash=json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reissue 标记 workflow_operation committed 失败 key=%s: %s", request.idempotency_key, type(exc).__name__)

    async def _post_execute(
        self,
        runtime: Any,
        run_context: Any,
        request: ReissueActionRequest,
        result: ReissueExecutionResult,
    ) -> None:
        """审批执行后的收尾。

        只负责动作终态 + 审计，不再迁移工单状态机：发起时已把工单迁到 AWAITING_APPROVAL，
        「审批通过/拒绝/取消」的 domain 状态迁移由 /chat/resume 等外部审批通道负责
        （AWAITING_APPROVAL→APPROVE/REJECT/CANCEL，均合法）。失败时仅把动作置 failed
        终态（registry 已 FAILED），工单仍停留在 AWAITING_APPROVAL（可接管/可仲裁）。
        """
        if not result.ok:
            await self._mark_operation_failed(
                runtime,
                run_context,
                request,
                result.error_code or "reissue_failed",
            )
            return
        # #1 C1：成功也落 workflow_operation committed 终态（机器可读终态 + 持久幂等锚点）。
        await self._mark_operation_committed(runtime, request, result)

    async def _transition_ticket(
        self,
        runtime: Any,
        run_context: Any,
        request: ReissueActionRequest,
        action: TicketAction,
    ) -> None:
        await self._apply_ticket_transition(runtime, run_context, request, action)

    async def _apply_ticket_transition(
        self,
        runtime: Any,
        run_context: Any,
        request: ReissueActionRequest,
        action: TicketAction,
    ) -> None:
        tickets = getattr(runtime, "tickets", None)
        if tickets is None or not hasattr(tickets, "get") or not hasattr(tickets, "transition"):
            return
        tenant_id = request.tenant_id
        try:
            ticket = await tickets.get(tenant_id, request.ticket_id)
            if ticket is None:
                return
            actor_id = getattr(run_context, "user_id", None) or "vpn-agent"
            scopes = set(getattr(run_context, "scopes", frozenset()) or ())
            # 仅当当前状态允许该动作时才迁移（避免非法跳转）；不匹配则静默跳过（非阻断）。
            current_status = ticket.status
            try:
                transition_ticket(
                    current_status,
                    TicketCommand(
                        ticket_id=request.ticket_id,
                        action=action,
                        actor_type=ActorType.AGENT,
                        actor_id=actor_id,
                        expected_version=int(getattr(ticket, "version", 0) or 0),
                        payload={"action": request.action.value, "idempotency_key": request.idempotency_key},
                    ),
                    scopes=scopes,
                )
            except Exception:  # 非法/无权跳转：不动工单，不阻断主流程
                return
            await tickets.transition(
                tenant_id,
                TicketCommand(
                    ticket_id=request.ticket_id,
                    action=action,
                    actor_type=ActorType.AGENT,
                    actor_id=actor_id,
                    expected_version=int(getattr(ticket, "version", 0) or 0),
                    payload={"action": request.action.value, "idempotency_key": request.idempotency_key},
                ),
                scopes=scopes,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reissue 工单状态迁移失败 ticket=%s action=%s: %s", request.ticket_id, action, type(exc).__name__)


def build_reissue_request(
    *,
    tenant_id: str,
    user_id: str,
    ticket_id: str,
    client_version: str,
    asset_id: str | None = None,
    idempotency_key: str | None = None,
    expected_version: int = 0,
    reason_codes: list[str] | None = None,
) -> ReissueActionRequest:
    """构造 reissue 执行请求（幂等键缺省自动生成）。"""
    key = idempotency_key or build_reissue_idempotency_key(
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=client_version,
    )
    return ReissueActionRequest(
        tenant_id=tenant_id,
        user_id=user_id,
        asset_id=asset_id,
        ticket_id=ticket_id,
        client_version=client_version,
        idempotency_key=key,
        expected_version=expected_version,
        reason_codes=list(reason_codes or []),
    )
