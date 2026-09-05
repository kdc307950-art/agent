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
    _audit,
    _operation_result,
    _operation_to_request,
    _request_id,
    approve_reissue,
    build_reissue_idempotency_key,
    cancel_reissue,
    classify_reissue_outcome,
    execute_approved_reissue,
    get_default_reissue_store,
    reject_reissue,
    start_reissue_approval,
)
from .outbox import InMemoryReissueOutbox, ReissueEventType
from .reissue_store import InMemoryReissueStore

logger = logging.getLogger("langgraph.vpn")

# 该动作在 workflow_operation 里的 command_type（对齐 audit / 幂等锚点）。
REISSUE_COMMAND_TYPE = "reissue_vpn_config"


class VpnReissueService:
    """审批式执行的运行时编排服务。

    不持有 runtime 引用（避免装配期循环依赖）：所有方法在执行时显式接收 runtime/run_context。
    单测可注入独立 ReissueRegistry 与 fake runtime（audit/tickets/vpn_adapter）。
    """

    def __init__(
        self,
        *,
        registry: ReissueRegistry | None = None,
        store: Any | None = None,
        outbox: Any | None = None,
    ) -> None:
        # #2：默认共享模块级登记表（与 reissue_vpn_config 工具同一实例），消除双入口不一致。
        # 存储：优先注入 store（Postgres/自定义）；否则内存实现包裹传入 registry；
        # 均未给则复用共享缺省 store（与工具入口共享同一实例），保证两执行入口状态一致。
        if store is not None:
            self.store = store
        elif registry is not None:
            self.store = InMemoryReissueStore(registry=registry)
        else:
            self.store = get_default_reissue_store()
        self.registry = (
            getattr(self.store, "registry", None) or registry or DEFAULT_REISSUE_REGISTRY
        )
        # Outbox 发布器：默认内存实现（无数据库也能记录事件流，供测试断言）；
        # 生产注入 PostgresReissueOutbox(pool) 以写入真实 outbox_events（复用通用 worker）。
        self.outbox = outbox if outbox is not None else InMemoryReissueOutbox()

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
            action=request.action,
            registry=self.registry,
            store=self.store,
        )
        status = outcome.get("status")
        if status == ApprovalStatus.PENDING.value:
            await self._record_operation_started(runtime, run_context, request)
            await self._transition_ticket(
                runtime, run_context, request, TicketAction.REQUEST_APPROVAL
            )
            await self._publish_outbox(
                ReissueEventType.OPERATION_STARTED,
                tenant_id=tenant_id,
                operation_id=request.idempotency_key,
                request=request,
                status_value="pending",
            )
        elif status == ApprovalStatus.FAILED.value:
            await self._mark_operation_failed(
                runtime,
                run_context,
                request,
                "preflight_failed",
                fail_reasons=outcome.get("fail_reasons") or [],
            )
            await self._publish_outbox(
                ReissueEventType.OPERATION_FAILED,
                tenant_id=tenant_id,
                operation_id=request.idempotency_key,
                request=request,
                status_value="failed",
                payload_init={
                    "error_code": "preflight_failed",
                    "fail_reasons": outcome.get("fail_reasons") or [],
                },
            )
        return outcome

    async def approve(
        self,
        *,
        operation_id: str | None = None,
        request: ReissueActionRequest | None = None,
        approver_user_id: str,
        runtime: Any,
        run_context: Any,
        decision: str = "approve",
    ) -> ReissueExecutionResult:
        """审批人批准：经 approval.approve_reissue 置 APPROVED（仅置状态），随后受控执行。

        - HTTP 可见入口只接受 operation_id（+ decision）；向后兼容仍接受 request。
          请求内容一律从 store 按 operation_id 重建（request_snapshot），不信任调用方/审批人入参。
        - approval.approve_reissue 不再自动执行；本服务在置 APPROVED 成功后调用 execute 以产生
          CONFIRMED/DELIVERED（保留既有 service.approve 行为，测试/调用方无需改动）。

        幂等：同幂等键已交付/确认直接返回既有结果，不重复执行（重复审批/重复回调不重复执行）。
        """
        if decision and decision != "approve":
            return ReissueExecutionResult(
                ok=False,
                status=ApprovalStatus.PENDING,
                idempotency_key=operation_id
                or (request.idempotency_key if request is not None else ""),
                reason=f"不支持的审批决策: {decision}",
                error_code="unsupported_decision",
            )
        op_id = operation_id or (request.idempotency_key if request is not None else None)
        if op_id is None:
            raise ValueError("approve 需要提供 operation_id 或 request")
        tenant_id = getattr(run_context, "tenant_id", None) or (
            request.tenant_id if request is not None else None
        )
        result = await approve_reissue(
            tenant_id=tenant_id,
            operation_id=op_id,
            runtime=runtime,
            run_context=run_context,
            approver_user_id=approver_user_id,
            store=self.store,
        )
        if result.ok and result.status == ApprovalStatus.APPROVED:
            op = await self.store.get_operation(tenant_id=tenant_id, operation_id=op_id)
            req = _operation_to_request(op) if op is not None else None
            if req is None:
                return result
            await self._publish_outbox(
                ReissueEventType.OPERATION_APPROVED,
                tenant_id=tenant_id,
                operation_id=op_id,
                request=req,
                status_value="approved",
                payload_init={"approver_user_id": approver_user_id},
            )
            exec_result = await self.execute(request=req, runtime=runtime, run_context=run_context)
            return exec_result
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
            store=self.store,
        )
        await self._mark_operation_failed(runtime, run_context, request, "rejected")
        return result

    async def confirm_submission(
        self,
        *,
        operation_id: str,
        submission_state: str,
        vendor_task_id: int | None,
        note: str | None,
        confirmer_user_id: str,
        runtime: Any,
        run_context: Any,
    ) -> dict[str, Any]:
        """记录人工核对的 FMG 提交事实，并只读对账，不重新提交 install。"""
        tenant_id = getattr(run_context, "tenant_id", None)
        op = await self.store.get_operation(tenant_id=tenant_id, operation_id=operation_id)
        if op is None:
            return {"ok": False, "error_code": "not_found", "operation_id": operation_id}
        if op.status not in (
            ApprovalStatus.EXECUTION_UNKNOWN,
            ApprovalStatus.RECONCILIATION_REQUIRED,
        ):
            return {
                "ok": False,
                "error_code": "illegal_transition",
                "operation_id": operation_id,
                "status": op.status.value,
                "reason": "只有 execution_unknown 或 reconciliation_required 允许人工确认提交",
            }
        if submission_state not in ("submitted", "not_submitted"):
            return {"ok": False, "error_code": "invalid_submission_state"}
        if submission_state == "submitted" and (not isinstance(vendor_task_id, int) or vendor_task_id <= 0):
            return {"ok": False, "error_code": "vendor_task_id_required"}
        if submission_state == "not_submitted" and vendor_task_id is not None:
            return {"ok": False, "error_code": "vendor_task_id_forbidden"}

        observation = {
            "vendor_system": op.vendor_system or "fortimanager",
            "submission_state": submission_state,
            "vendor_task_id": vendor_task_id,
            "manual_confirmation": True,
            "confirmed_by": confirmer_user_id,
        }
        if note:
            observation["note"] = note
        saved = await self.store.record_submission_observation(
            tenant_id=tenant_id,
            operation_id=operation_id,
            submission_state=submission_state,
            vendor_task_id=vendor_task_id,
            external_result=observation,
        )
        if not saved:
            return {"ok": False, "error_code": "not_found", "operation_id": operation_id}
        request = _operation_to_request(op)
        if request is None:
            return {
                "ok": False,
                "error_code": "missing_request_snapshot",
                "operation_id": operation_id,
            }
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_submission_confirmed",
            status="observed",
            payload={
                **_request_id(request),
                "submission_state": submission_state,
                "vendor_task_id": vendor_task_id,
                "confirmed_by": confirmer_user_id,
                "note": note,
            },
        )
        if submission_state == "not_submitted":
            await self.store.set_status(
                tenant_id=tenant_id,
                operation_id=operation_id,
                from_status=op.status,
                to_status=ApprovalStatus.FAILED,
                error_code="not_submitted_confirmed",
            )
            await self._mark_operation_failed(runtime, run_context, request, "not_submitted_confirmed")
            await self._publish_outbox(
                ReissueEventType.OPERATION_FAILED,
                tenant_id=tenant_id,
                operation_id=operation_id,
                request=request,
                status_value="failed",
                payload_init={"error_code": "not_submitted_confirmed", "manual": True},
            )
            return {
                "ok": True,
                "operation_id": operation_id,
                "status": ApprovalStatus.FAILED.value,
                "submission_state": submission_state,
            }

        # submitted 只进入已有 get_task_status/reconcile 读路径，绝不再次 install。
        result = await self.reconcile(
            idempotency_key=operation_id, runtime=runtime, run_context=run_context
        )
        result["submission_state"] = submission_state
        result["vendor_task_id"] = vendor_task_id
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
            store=self.store,
        )
        await self._mark_operation_failed(runtime, run_context, request, "cancelled")
        return result

    async def get_result(
        self, *, idempotency_key: str, tenant_id: str | None = None
    ) -> ReissueExecutionResult | None:
        # ITEM 5：状态/结果读经 store（存储为唯一权威）；未给 tenant 时回退登记表（向后兼容）。
        if tenant_id is not None:
            op = await self.store.get_by_idempotency_key(
                tenant_id=tenant_id, idempotency_key=idempotency_key
            )
            if op is not None:
                result = _operation_result(op)
                if result is not None:
                    return result
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
        tenant_id = request.tenant_id
        op_id = request.idempotency_key
        result = await execute_approved_reissue(
            request=request,
            runtime=runtime,
            run_context=run_context,
            store=self.store,
        )
        await self._post_execute(runtime, run_context, request, result)
        # 生命周期事件（每转换一事件；幂等键去重，重复不重复入箱）：
        #   execution_started <- 进入 EXECUTING；终态按 result.status 归因到 4 类之一。
        if result.error_code not in ("not_approved", "preflight_failed"):
            await self._publish_outbox(
                ReissueEventType.EXECUTION_STARTED,
                tenant_id=tenant_id,
                operation_id=op_id,
                request=request,
                status_value="executing",
            )
        if result.status == ApprovalStatus.EXECUTION_UNKNOWN:
            await self._publish_outbox(
                ReissueEventType.EXECUTION_UNKNOWN,
                tenant_id=tenant_id,
                operation_id=op_id,
                request=request,
                status_value="execution_unknown",
                payload_init={"error_code": result.error_code},
            )
        elif result.status in (ApprovalStatus.CONFIRMED, ApprovalStatus.DELIVERED):
            await self._publish_outbox(
                ReissueEventType.OPERATION_CONFIRMED,
                tenant_id=tenant_id,
                operation_id=op_id,
                request=request,
                status_value=result.status.value,
                payload_init={
                    "ok": result.ok,
                    "delivered": result.delivered,
                    "confirmed": result.confirmed,
                },
            )
        elif result.status == ApprovalStatus.FAILED:
            await self._publish_outbox(
                ReissueEventType.OPERATION_FAILED,
                tenant_id=tenant_id,
                operation_id=op_id,
                request=request,
                status_value="failed",
                payload_init={"error_code": result.error_code},
            )
        return result

    # ---- 补偿对账（阶段五：收敛 execution_unknown / reconciliation_required） ----

    async def reconcile(
        self,
        *,
        idempotency_key: str,
        runtime: Any,
        run_context: Any,
    ) -> dict[str, Any]:
        """补偿对账：对外部结果未知/本地落库失败的执行，重查外部真实结果并收敛到定态。

        流程（供 worker 逐键调用；无需外部入参，请求上下文取自 registry 快照）：
            1. 仅对 EXECUTION_UNKNOWN / RECONCILIATION_REQUIRED 进行对账；
            2. 幂等重放外部 reissue_config（同键幂等，返回权威真实结果）；
            3. 分类真实结果：
               - delivered/confirmed       → 补写 workflow_operation committed + 工单迁移(APPROVE)
                                            + 置 CONFIRMED/DELIVERED + 审计 vpn_reissue_reconciled；
               - 外部明确未下发             → 补写 workflow_operation failed + 置 FAILED + 审计；
               - 仍未知/再次超时           → 保持 EXECUTION_UNKNOWN + 审计（下轮再对账）。
        返回：{"idempotency_key", "reconciled", "status", "external_result"}。
        """
        tenant_id = getattr(run_context, "tenant_id", None)
        op = (
            await self.store.get_by_idempotency_key(
                tenant_id=tenant_id, idempotency_key=idempotency_key
            )
            if tenant_id
            else None
        )
        if op is None:
            current = self.registry.get_status(idempotency_key)
            if current is None:
                return {
                    "idempotency_key": idempotency_key,
                    "reconciled": False,
                    "status": None,
                    "reason": "missing_operation",
                }
            return {
                "idempotency_key": idempotency_key,
                "reconciled": False,
                "status": current.value,
                "reason": "not_reconcilable",
            }
        current = op.status
        if current not in (
            ApprovalStatus.EXECUTION_UNKNOWN,
            ApprovalStatus.RECONCILIATION_REQUIRED,
        ):
            return {
                "idempotency_key": idempotency_key,
                "reconciled": False,
                "status": current.value,
                "reason": "not_reconcilable",
            }
        request = _operation_to_request(op)
        if request is None:
            return {
                "idempotency_key": idempotency_key,
                "reconciled": False,
                "status": current.value,
                "reason": "missing_request_snapshot",
            }
        operation_id = op.operation_id
        outcome = await self._query_external(runtime, request, external_result=op.external_result)
        outcome_status, error_code = classify_reissue_outcome(outcome)
        base = {**_request_id(request), "external_result": outcome}

        if outcome_status in (ApprovalStatus.CONFIRMED, ApprovalStatus.DELIVERED):
            # 外部已成功：补写终态 + 工单迁移 + 审计（收敛，不再依赖单次请求完成）。
            result = ReissueExecutionResult(
                ok=True,
                action=request.action,
                status=outcome_status,
                idempotency_key=idempotency_key,
                delivered=True,
                confirmed=(outcome_status == ApprovalStatus.CONFIRMED),
                detail=outcome,
            )
            await self.store.set_result(
                tenant_id=tenant_id, operation_id=operation_id, result=result
            )
            await self._mark_operation_committed(runtime, request, result)
            await self._apply_ticket_transition(
                runtime, run_context, request, TicketAction.APPROVE, ActorType.APPROVER
            )
            await _audit(
                runtime,
                run_context,
                "vpn_reissue_reconciled",
                status=outcome_status.value,
                payload={
                    **base,
                    "reason": "external_success_reconciled",
                    "result": result.model_dump(mode="json"),
                },
            )
            await self._publish_outbox(
                ReissueEventType.RECONCILED,
                tenant_id=tenant_id,
                operation_id=operation_id,
                request=request,
                status_value=outcome_status.value,
                payload_init={"reason": "external_success_reconciled"},
            )
            return {
                "idempotency_key": idempotency_key,
                "reconciled": True,
                "status": outcome_status.value,
                "external_result": outcome,
            }

        if outcome_status == ApprovalStatus.EXECUTION_UNKNOWN:
            # 仍未知：保持 execution_unknown，审计后下轮再对账。
            await _audit(
                runtime,
                run_context,
                "vpn_reissue_reconciliation_pending",
                status="execution_unknown",
                payload={**base, "reason": "external_still_unknown"},
            )
            return {
                "idempotency_key": idempotency_key,
                "reconciled": False,
                "status": ApprovalStatus.EXECUTION_UNKNOWN.value,
                "external_result": outcome,
            }

        # 外部明确未下发：补写 failed + 工单回可接管 + 审计。
        await self.store.set_status(
            tenant_id=tenant_id,
            operation_id=operation_id,
            from_status=current,
            to_status=ApprovalStatus.FAILED,
            error_code=error_code or "reissue_failed",
        )
        await self._mark_operation_failed(
            runtime, run_context, request, error_code or "reissue_failed"
        )
        await _audit(
            runtime,
            run_context,
            "vpn_reissue_failed",
            status="failed",
            payload={
                **base,
                "reason": "external_definitely_not_delivered",
                "error_code": error_code,
            },
        )
        await self._publish_outbox(
            ReissueEventType.RECONCILED,
            tenant_id=tenant_id,
            operation_id=operation_id,
            request=request,
            status_value="failed",
            payload_init={"reason": "external_definitely_not_delivered", "error_code": error_code},
        )
        return {
            "idempotency_key": idempotency_key,
            "reconciled": True,
            "status": ApprovalStatus.FAILED.value,
            "external_result": outcome,
        }

    async def reconcile_all(self, *, runtime: Any, run_context: Any) -> list[dict[str, Any]]:
        """仅扫描当前租户的待对账操作（供人工按租户触发）。

        后台 worker 必须调用 ``claim_reconcilable``，由 runtime 为每条操作构造所属
        租户的系统上下文；这里不能跨租户扫描，以免泄露 operation_id 或用错租户上下文。
        """
        results: list[dict[str, Any]] = []
        ops = await self.store.list_reconcilable(limit=1000)
        for op in ops:
            if op.tenant_id != getattr(run_context, "tenant_id", None):
                continue
            results.append(
                await self.reconcile(
                    idempotency_key=op.idempotency_key, runtime=runtime, run_context=run_context
                )
            )
        return results

    async def _publish_outbox(
        self,
        event_type: ReissueEventType,
        *,
        tenant_id: str,
        operation_id: str,
        request: ReissueActionRequest | None,
        status_value: str | None = None,
        payload_init: dict[str, Any] | None = None,
    ) -> None:
        """发布一条 reissue 生命周期 Outbox 事件（幂等；失败不阻断主流程但记录警告）。

        只在注入/默认 outbox 存在时发布；同一 (operation_id, event_type) 由幂等键去重，
        保证「每转换一事件」。发布失败记 warning，不静默丢弃（不吞掉投递管道的故障）。
        """
        if self.outbox is None:
            return
        base = {"idempotency_key": operation_id} if request is None else _request_id(request)
        payload: dict[str, Any] = dict(base)
        if status_value is not None:
            payload["status"] = status_value
        if payload_init:
            payload.update(payload_init)
        try:
            await self.outbox.publish(
                tenant_id=tenant_id,
                operation_id=operation_id,
                event_type=event_type,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001  发布失败不阻断主流程，仅告警
            logger.warning(
                "reissue outbox 事件发布失败 event=%s op=%s: %s",
                event_type,
                operation_id,
                type(exc).__name__,
            )

    async def _query_external(
        self,
        runtime: Any,
        request: ReissueActionRequest,
        *,
        external_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """查询已提交的外部 task；没有 task id 才回退到非生产 mock 的幂等重放。"""
        command_gateway = getattr(runtime, "vpn_command_gateway", None)
        task_id = (external_result or {}).get("vendor_task_id")
        if command_gateway is not None:
            if not isinstance(task_id, int) or task_id <= 0:
                return {
                    "delivered": False,
                    "confirmed": False,
                    "error_code": "external_result_unknown",
                    "reason": "missing_vendor_task_id",
                }
            try:
                return await command_gateway.get_command_status(
                    tenant_id=request.tenant_id,
                    task_id=task_id,
                    external_request_id=(external_result or {}).get("external_request_id"),
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "delivered": False,
                    "confirmed": False,
                    "error_code": "execution_unknown",
                    "reason": type(exc).__name__,
                    "vendor_task_id": task_id,
                }
        adapter = getattr(runtime, "vpn_adapter", None)
        if adapter is None or not hasattr(adapter, "reissue_config"):
            return {
                "delivered": False,
                "confirmed": False,
                "error_code": "external_result_unknown",
                "reason": "adapter_unavailable",
            }
        try:
            return await adapter.reissue_config(
                user_id=request.user_id,
                idempotency_key=request.idempotency_key,
                target_version=request.client_version,
            )
        except Exception as exc:  # noqa: BLE001  重查异常 → 仍未知
            return {
                "delivered": False,
                "confirmed": False,
                "error_code": "execution_unknown",
                "reason": type(exc).__name__,
            }

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
            logger.warning(
                "reissue 登记 workflow_operation 失败 key=%s: %s",
                request.idempotency_key,
                type(exc).__name__,
            )

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
            logger.warning(
                "reissue 标记 workflow_operation 失败 key=%s: %s",
                request.idempotency_key,
                type(exc).__name__,
            )

    async def _mark_operation_committed(
        self,
        runtime: Any,
        request: ReissueActionRequest,
        result: ReissueExecutionResult,
    ) -> bool:
        """落 workflow_operation committed 终态；返回是否成功（阶段五：本地提交失败需对账）。"""
        tickets = getattr(runtime, "tickets", None)
        if tickets is None or not hasattr(tickets, "mark_workflow_operation_committed"):
            return False
        try:
            await tickets.mark_workflow_operation_committed(
                tenant_id=request.tenant_id,
                ticket_id=request.ticket_id,
                operation_id=request.idempotency_key,
                result_hash=json.dumps(
                    result.model_dump(mode="json"), ensure_ascii=False, default=str
                ),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reissue 标记 workflow_operation committed 失败 key=%s: %s",
                request.idempotency_key,
                type(exc).__name__,
            )
            return False

    async def _post_execute(
        self,
        runtime: Any,
        run_context: Any,
        request: ReissueActionRequest,
        result: ReissueExecutionResult,
    ) -> None:
        """审批执行后的收尾（可恢复状态模型，阶段五）。

        只负责动作终态 + 审计，不再迁移工单状态机：发起时已把工单迁到 AWAITING_APPROVAL，
        「审批通过/拒绝/取消」的 domain 状态迁移由 /chat/resume 等外部审批通道负责
        （AWAITING_APPROVAL→APPROVE/REJECT/CANCEL，均合法）。

        关键差异（取代「异常记录后继续」）：执行**成功**但本地 workflow_operation 落库失败时，
        不再简单吞掉异常误判为成功，而是转 **RECONCILIATION_REQUIRED** 交由补偿对账收敛。
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
        committed = await self._mark_operation_committed(runtime, request, result)
        if not committed:
            # 外部已下发但本地落库失败 → 保持可恢复状态，交对账补写（不再吞错）。
            await self.store.set_status(
                tenant_id=request.tenant_id,
                operation_id=request.idempotency_key,
                from_status=result.status,
                to_status=ApprovalStatus.RECONCILIATION_REQUIRED,
            )
            await _audit(
                runtime,
                run_context,
                "vpn_reissue_reconciliation_required",
                status="reconciliation_required",
                payload={
                    **_request_id(request),
                    "reason": "local_commit_failed",
                    "result": result.model_dump(mode="json"),
                },
            )

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
        actor_type: ActorType = ActorType.AGENT,
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
                        actor_type=actor_type,
                        actor_id=actor_id,
                        expected_version=int(getattr(ticket, "version", 0) or 0),
                        payload={
                            "action": request.action.value,
                            "idempotency_key": request.idempotency_key,
                        },
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
                    actor_type=actor_type,
                    actor_id=actor_id,
                    expected_version=int(getattr(ticket, "version", 0) or 0),
                    payload={
                        "action": request.action.value,
                        "idempotency_key": request.idempotency_key,
                    },
                ),
                scopes=scopes,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reissue 工单状态迁移失败 ticket=%s action=%s: %s",
                request.ticket_id,
                action,
                type(exc).__name__,
            )


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
