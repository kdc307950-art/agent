"""LANGGraph VPN 客户处置闭环 —— 服务编排：诊断落库 + 状态机联动 + 客户动作闭环。

模块归属：backend/vpn。设计目标（复用既有实现，不重复造轮子）：
    - 复用 backend/vpn/service.VpnDiagnosisService.run_with_context 执行「上下文组装 ->
      Agent 生成 -> 诊断门禁」，得到 result（含 DiagnosisCommand / must_handoff / evaluation）。
    - 在其上补齐阶段二闭环：把 result 落为 VpnDiagnosisRun（含 evidence/ruled_out/下一步），
      把 provide_steps 草稿解析为 VpnCustomerAction 列表并持久化，联动 domain 状态机
      （START_DIAGNOSIS -> diagnosing / PRESCRIBE_STEPS -> awaiting_customer_action /
      PROVIDE_ACTION_RESULT -> diagnosing / REQUEST_RECONCILIATION -> reconciliation_required，
      复用 src/my_agent/helpdesk.domain.transition_ticket + tickets.transition）。
    - 复用 backend/vpn/executor.execute_diagnosis_command 处理除 provide_steps 以外的
      命令（ask_customer / assign_agent / escalate_incident / request_approval）。
    - 每次关键点写 audit.record_event（vpn_diagnosis_*），payload 关联 tenant/ticket/run/action。

红线：本服务只做「诊断落库 + 状态迁移 + 给客户步骤」，不代发客户消息、不执行
账号/权限/网关/关单等副作用（仍由 executor 与 FORBIDDEN_COMMANDS 封死）。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from src.my_agent.helpdesk import (
    ActorType,
    InvalidTicketTransition,
    TicketAction,
    TicketCommand,
    TicketPermissionDenied,
    transition_ticket,
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
from .executor import execute_diagnosis_command
from .models import DiagnosisCommand

logger = logging.getLogger("langgraph.vpn")

# 升级记录默认人工接管队列（对齐 executor.HUMAN_QUEUE_TEAM_ID）。
HUMAN_QUEUE_TEAM_ID = "team-service-desk"

# 识别「诊断结论需人工/再次处置」的处置命令。
HANDOFF_COMMANDS = frozenset({"escalate_incident", "request_approval"})
CREATE_AGENT_ACTIONS = frozenset({TicketAction.START_DIAGNOSIS, TicketAction.PRESCRIBE_STEPS})


def _next_action_from_command(command: Any) -> str | None:
    """从 DiagnosisCommand dict 提取 next_action（命令值）；非法/None 返回 None。"""
    if not isinstance(command, dict):
        return None
    value = command.get("command")
    return str(value) if value else None


class VpnDiagnosisNotFound(LookupError):
    """目标工单不存在或不在可诊断状态。"""


class VpnCustomerActionError(ValueError):
    """客户动作结果提交不符合预期（动作不存在 / 不属于当前工单 / 重复提交）。"""


class VpnPersistenceError(RuntimeError):
    """VPN 闭环关键数据持久化到 PostgreSQL 失败（落库异常向上冒泡，避免被静默吞掉）。

    仅在 self.repository 存在（生产、有库）时抛出；repository 为 None（单测/内存版）保持原逻辑不抛。
    """


class VpnClosedLoopService:
    """VPN 客户处置闭环编排服务（诊断落库 + 状态机联动 + 客户动作闭环）。

    不持有 runtime 引用（避免装配期循环依赖）：所有方法在执行时显式接收 runtime/run_context。
    单测可注入独立 DiagnosisRegistry 与 stub diagnosis_service。
    """

    def __init__(
        self,
        *,
        diagnosis_service: Any,
        registry: DiagnosisRegistry | None = None,
        repository: Any | None = None,
    ) -> None:
        self.diagnosis_service = diagnosis_service
        self.registry = registry or DiagnosisRegistry()
        # PostgreSQL 持久化仓储（方案 A）：生产环境 runtime 提供时镜像落库；缺省 None 时仅内存登记表。
        self.repository = repository

    # ---- 公有编排入口 ----

    async def diagnose(
        self,
        *,
        runtime: Any,
        tenant_id: str,
        ticket_id: str,
        run_context: Any,
    ) -> dict[str, Any]:
        """执行一次 VPN 诊断并落库、联动状态机。

        返回 {"run": ..., "result": ..., "dispatch": ...}。
        """
        await self._ensure_ticket(runtime, tenant_id, ticket_id)
        outcome = await self.diagnosis_service.run_with_context(
            runtime=runtime,
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            run_context=run_context,
        )
        result = outcome.get("result") if isinstance(outcome, dict) else None
        if not isinstance(result, dict):
            result = {}
        run = self._build_run(outcome, tenant_id=tenant_id, ticket_id=ticket_id)
        self.registry.save_run(run)
        await self._persist_run(run)

        await self._audit(
            runtime,
            run_context,
            "vpn_diagnosis_started",
            status=run.status.value,
            payload={
                "tenant_id": tenant_id,
                "ticket_id": ticket_id,
                "run_id": run.run_id,
                "fault": run.fault,
                "confidence": run.confidence,
                "next_action": run.next_action,
                "must_handoff": bool(result.get("must_handoff")),
            },
        )
        await self._append_timeline(
            runtime,
            run_context,
            tenant_id,
            ticket_id,
            action="vpn_diagnosis",
            payload={
                "run_id": run.run_id,
                "fault": run.fault,
                "confidence": run.confidence,
                "next_action": run.next_action,
                "must_handoff": bool(result.get("must_handoff")),
            },
        )

        # 进入/保持诊断状态（非阻断：已在 diagnosing / 非法则不迁移）。
        await self._transition(
            runtime,
            run_context,
            tenant_id,
            ticket_id,
            TicketAction.START_DIAGNOSIS,
            payload={"run_id": run.run_id},
        )

        dispatch = await self._dispatch(result, runtime, run_context, tenant_id, ticket_id, run)
        await self._persist_run(run)  # dispatch 可能推进 run.status，回写一次落库
        return {
            "run": run.model_dump(mode="json"),
            "result": result,
            "dispatch": dispatch,
        }

    async def resume(
        self,
        *,
        runtime: Any,
        tenant_id: str,
        ticket_id: str,
        run_context: Any,
    ) -> dict[str, Any]:
        """再次诊断（新 run）：前置诊断运行置 COMPLETED/超期，再开新一轮。"""
        # PostgreSQL 单一事实来源：repository 存在时读库，否则回退内存登记表（单测）。
        if self.repository is not None:
            prev = await self.repository.get_latest_run(tenant_id, ticket_id)
        else:
            prev = self.registry.get_latest_run(ticket_id)
        if prev is not None and prev.status == DiagnosisRunStatus.DIAGNOSING:
            prev.status = DiagnosisRunStatus.COMPLETED
            prev.updated_at = datetime.now(UTC)
        return await self.diagnose(
            runtime=runtime, tenant_id=tenant_id, ticket_id=ticket_id, run_context=run_context
        )

    async def submit_action_result(
        self,
        *,
        runtime: Any,
        tenant_id: str,
        ticket_id: str,
        action_id: str,
        result: str,
        run_context: Any,
        evidence: dict[str, Any] | None = None,
        details: str = "",
    ) -> dict[str, Any]:
        """客户回填一条排查步骤的执行结果，并联动状态机 + 再次诊断。

        返回 {"action_result": ..., "re_diagnosis": ...}。
        """
        # PostgreSQL 单一事实来源：repository 存在时从库读取并校验（跨租户/跨工单返回 None），
        # 否则回退内存登记表（单测/无库）。读取/落库失败向 API 冒泡，不静默吞掉。
        if self.repository is not None:
            try:
                action = await self.repository.get_action(tenant_id, ticket_id, action_id)
            except Exception as exc:  # noqa: BLE001
                raise VpnPersistenceError(
                    f"VPN 排查步骤读取失败 action={action_id}: {type(exc).__name__}"
                ) from exc
        else:
            action = self.registry.get_action(action_id)
        if action is None or action.ticket_id != ticket_id or action.tenant_id != tenant_id:
            raise VpnCustomerActionError("该排查步骤不存在或不属于当前工单")
        if action.status == CustomerActionStatus.EXECUTED:
            raise VpnCustomerActionError("该排查步骤已回填结果，请勿重复提交")

        actor_id = getattr(run_context, "user_id", None) or "customer"
        created = VpnCustomerActionResult(
            action_id=action_id,
            ticket_id=ticket_id,
            tenant_id=tenant_id,
            run_id=action.run_id,
            result=result,
            evidence=evidence or {},
            details=details,
            submitted_by=actor_id,
        )
        self.registry.add_action_result(created)
        await self._persist_action_result(created)
        # 动作状态推进为 EXECUTED（内存 + PostgreSQL）。
        await self._persist_action_status(
            tenant_id, ticket_id, action_id, CustomerActionStatus.EXECUTED
        )

        await self._audit(
            runtime,
            run_context,
            "vpn_customer_action_result",
            status="submitted",
            payload={
                "tenant_id": tenant_id,
                "ticket_id": ticket_id,
                "action_id": action_id,
                "run_id": created.run_id,
                "submitted_by": actor_id,
            },
        )
        await self._append_timeline(
            runtime,
            run_context,
            tenant_id,
            ticket_id,
            action="vpn_customer_action",
            actor_type="customer",
            payload={
                "action_id": action_id,
                "result_id": created.result_id,
                "submitted_by": actor_id,
            },
        )

        # 状态机：awaiting_customer_action -> diagnosing（客户提供动作结果）。
        transitioned = await self._transition(
            runtime,
            run_context,
            tenant_id,
            ticket_id,
            TicketAction.PROVIDE_ACTION_RESULT,
            payload={"action_id": action_id},
        )

        # 再次诊断：只有成功迁移到 diagnosing 才重跑，否则仅落结果不重跑。
        re_diagnosis: dict[str, Any] | None = None
        if transitioned:
            re_diagnosis = await self.diagnose(
                runtime=runtime,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                run_context=run_context,
            )
            # 若再次诊断仍需转人工/无命令，则请求对账（reconciliation）。
            re_diag_result = re_diagnosis.get("result") or {}
            if re_diag_result.get("must_handoff") or not re_diag_result.get("command"):
                await self._transition(
                    runtime,
                    run_context,
                    tenant_id,
                    ticket_id,
                    TicketAction.REQUEST_RECONCILIATION,
                    payload={"reason": "客户动作后诊断仍未解决"},
                )

        return {
            "action_result": created.model_dump(mode="json"),
            "transition": transitioned,
            "re_diagnosis": re_diagnosis,
        }

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
    ) -> dict[str, Any]:
        """返回工单当前的 VPN 处置闭环快照（诊断/动作/结果/升级）。

        生产环境（repository 存在）优先读 PostgreSQL；否则回退到内存登记表（单测）。
        """
        if self.repository is not None:
            latest_run = await self.repository.get_latest_run(tenant_id, ticket_id)
            runs = await self.repository.list_runs(tenant_id, ticket_id)
            actions = await self.repository.list_actions(tenant_id, ticket_id)
            results = await self.repository.list_action_results(tenant_id, ticket_id)
            escalations = await self.repository.list_escalations(tenant_id, ticket_id)
        else:
            latest_run = self.registry.get_latest_run(ticket_id)
            runs = self.registry.list_runs(ticket_id)
            actions = self.registry.list_actions(ticket_id)
            results = self.registry.list_ticket_results(ticket_id)
            escalations = self.registry.list_escalations(ticket_id)
        return {
            "ticket_id": ticket_id,
            "latest_run": latest_run.model_dump(mode="json") if latest_run else None,
            "runs": [run.model_dump(mode="json") for run in runs],
            "actions": [action.model_dump(mode="json") for action in actions],
            "results": [result.model_dump(mode="json") for result in results],
            "escalations": [esc.model_dump(mode="json") for esc in escalations],
        }

    # ---- 内部：结果 -> VpnDiagnosisRun ----

    def _build_run(self, outcome: Any, *, tenant_id: str, ticket_id: str) -> VpnDiagnosisRun:
        request = outcome.get("request") if isinstance(outcome, dict) else None
        result = outcome.get("result") if isinstance(outcome, dict) else None
        if not isinstance(result, dict):
            result = {}
        request = request if isinstance(request, dict) else {}
        command = result.get("command")
        # 阶段三：优先用证据链结构化假设（service.apply_handoff 写入的 evidence_chain），
        # 使 VpnDiagnosisRun.hypothesis/evidence/ruled_out/confidence/next_action/reason_codes
        # 来自确定性规则 EvidenceDiagnosis，供 M3 fault_hypothesis 消费（而非 command.content）。
        evidence_chain = result.get("evidence_chain")
        if isinstance(evidence_chain, dict) and evidence_chain.get("hypothesis"):
            hypothesis = str(evidence_chain.get("hypothesis") or "")
            confidence = self._as_float(evidence_chain.get("confidence"))
            evidence = [
                VpnDiagnosisFinding(
                    tool_name="rules",
                    evidence=str(ev_item),
                    found=True,
                )
                for ev_item in (evidence_chain.get("evidence") or [])
            ]
            ruled_out = list(evidence_chain.get("ruled_out") or [])
            # 与 else 分支保持一致的同型（str | None），避免 mypy 因 if/else 分支类型不一致报错；
            # 同时避免 str(None)="None" 的边界（二者均缺时保留 None）。
            next_action = evidence_chain.get("next_action") or _next_action_from_command(command)
            reason_codes = list(evidence_chain.get("reason_codes") or [])
        else:
            # 回退：无结构化证据链时用命令维度（向后兼容）。
            next_action = _next_action_from_command(command)
            confidence = (
                self._as_float(result.get("evaluation", {}).get("confidence"))
                if isinstance(result.get("evaluation"), dict)
                else self._as_float(command.get("confidence")) if isinstance(command, dict) else 0.0
            )
            evidence = [
                VpnDiagnosisFinding(
                    tool_name=str(item.get("tool_name") or ""),
                    evidence=str(item.get("content") or ""),
                    document_id=item.get("document_id"),
                    document_version=item.get("document_version"),
                    chunk_id=item.get("chunk_id"),
                    title=item.get("title"),
                    found=bool(item.get("found", True)),
                )
                for item in (result.get("tool_evidence") or [])
            ]
            hypothesis = str(command.get("content") or "") if command else ""
            ruled_out = list(result.get("ruled_out") or [])
            reason_codes = list(command.get("reason_codes") or []) if command else []

        must_handoff = bool(result.get("must_handoff"))
        run_status = (
            DiagnosisRunStatus.HANDED_OFF
            if (must_handoff and next_action is None)
            else DiagnosisRunStatus.DIAGNOSING
        )

        return VpnDiagnosisRun(
            run_id=self._new_run_id(),
            ticket_id=ticket_id,
            tenant_id=tenant_id,
            fault=str(request.get("fault") or "connection_failed"),
            hypothesis=hypothesis,
            confidence=confidence,
            evidence=evidence[:64],
            ruled_out=ruled_out,
            next_action=next_action,
            reason_codes=reason_codes,
            status=run_status,
        )

    # ---- 内部：命令分派 ----

    async def _dispatch(
        self,
        result: dict[str, Any],
        runtime: Any,
        run_context: Any,
        tenant_id: str,
        ticket_id: str,
        run: VpnDiagnosisRun,
    ) -> dict[str, Any]:
        command = result.get("command")
        if command is None:
            # 无命令（must_handoff / 错误）：记录升级 + 请求对账。
            await self._record_escalation_and_reconcile(
                runtime, run_context, tenant_id, ticket_id, run, result
            )
            run.status = DiagnosisRunStatus.HANDED_OFF
            return {"ok": True, "command": None, "status": "handed_off"}

        if str(command.get("command")) == "provide_steps":
            draft = str(command.get("content") or "")
            actions = parse_steps_to_actions(
                draft,
                ticket_id=ticket_id,
                tenant_id=tenant_id,
                run_id=run.run_id,
            )
            for action in actions:
                self.registry.add_action(action)
                await self._persist_action(action)
            await self._audit(
                runtime,
                run_context,
                "vpn_diagnosis_prescribed_steps",
                status="issued",
                payload={
                    "ticket_id": ticket_id,
                    "run_id": run.run_id,
                    "count": len(actions),
                    "action_ids": [a.action_id for a in actions],
                },
            )
            await self._transition(
                runtime,
                run_context,
                tenant_id,
                ticket_id,
                TicketAction.PRESCRIBE_STEPS,
                payload={"run_id": run.run_id, "action_ids": [a.action_id for a in actions]},
            )
            await self._append_timeline(
                runtime,
                run_context,
                tenant_id,
                ticket_id,
                action="vpn_prescribed_steps",
                payload={"run_id": run.run_id, "action_ids": [a.action_id for a in actions]},
            )
            run.status = DiagnosisRunStatus.COMPLETED
            return {
                "ok": True,
                "command": "provide_steps",
                "status": "awaiting_customer_action",
                "actions": [a.model_dump(mode="json") for a in actions],
            }

        # 其它命令：复用 executor（ask_customer / assign_agent / escalate_incident / request_approval）。
        try:
            diagnosis_command = DiagnosisCommand.model_validate(command)
        except Exception as exc:  # noqa: BLE001
            logger.warning("VPN 命令解析失败: %s", type(exc).__name__)
            await self._record_escalation_and_reconcile(
                runtime, run_context, tenant_id, ticket_id, run, result
            )
            run.status = DiagnosisRunStatus.FAILED
            return {"ok": False, "status": "failed", "reason": "command_invalid"}

        exec_result = await execute_diagnosis_command(
            diagnosis_command,
            runtime=runtime,
            run_context=run_context,
            ticket_id=ticket_id,
        )
        if exec_result.ok:
            run.status = DiagnosisRunStatus.COMPLETED
            # escalate_incident 进入人工队列 => handed_off。
            if str(command.get("command")) == "escalate_incident":
                run.status = DiagnosisRunStatus.HANDED_OFF
                await self._record_escalation(
                    runtime, run_context, tenant_id, ticket_id, run, result
                )
        else:
            run.status = DiagnosisRunStatus.FAILED
        return {
            "ok": exec_result.ok,
            "command": command.get("command"),
            "exec": asdict(exec_result),
        }

    # ---- 内部：升级 / 对账 ----

    async def _record_escalation_and_reconcile(
        self,
        runtime: Any,
        run_context: Any,
        tenant_id: str,
        ticket_id: str,
        run: VpnDiagnosisRun,
        result: dict[str, Any],
    ) -> None:
        evaluation = result.get("evaluation") or {}
        reasons = list(evaluation.get("handoff_reasons") or [])
        await self._record_escalation(
            runtime,
            run_context,
            tenant_id,
            ticket_id,
            run,
            result,
            reasons=reasons,
        )
        await self._transition(
            runtime,
            run_context,
            tenant_id,
            ticket_id,
            TicketAction.REQUEST_RECONCILIATION,
            payload={"reason": "diagnosis_must_handoff", "handoff_reasons": reasons},
        )

    async def _record_escalation(
        self,
        runtime: Any,
        run_context: Any,
        tenant_id: str,
        ticket_id: str,
        run: VpnDiagnosisRun,
        result: dict[str, Any],
        *,
        reasons: list[str] | None = None,
    ) -> None:
        reasons = reasons or list((result.get("evaluation") or {}).get("handoff_reasons") or [])
        escalation = VpnEscalation(
            escalation_id=self._new_escalation_id(),
            ticket_id=ticket_id,
            tenant_id=tenant_id,
            run_id=run.run_id,
            reason=str(result.get("evaluation", {}).get("handoff_reasons") or reasons or []),
            reason_codes=reasons,
            target_queue=HUMAN_QUEUE_TEAM_ID,
            status=EscalationStatus.OPEN,
        )
        self.registry.add_escalation(escalation)
        await self._persist_escalation(escalation)
        await self._audit(
            runtime,
            run_context,
            "vpn_diagnosis_escalated",
            status="open",
            payload={
                "tenant_id": tenant_id,
                "ticket_id": ticket_id,
                "run_id": run.run_id,
                "escalation_id": escalation.escalation_id,
                "reason_codes": reasons,
            },
        )
        await self._append_timeline(
            runtime,
            run_context,
            tenant_id,
            ticket_id,
            action="vpn_escalated",
            payload={
                "run_id": run.run_id,
                "escalation_id": escalation.escalation_id,
                "reason_codes": reasons,
            },
        )

    # ---- 内部：状态机迁移（复用 domain.transition_ticket + tickets.transition） ----

    async def _transition(
        self,
        runtime: Any,
        run_context: Any,
        tenant_id: str,
        ticket_id: str,
        action: TicketAction,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        tickets = getattr(runtime, "tickets", None)
        if tickets is None or not hasattr(tickets, "get") or not hasattr(tickets, "transition"):
            return False
        try:
            ticket = await tickets.get(tenant_id, ticket_id)
            if ticket is None:
                raise VpnDiagnosisNotFound(f"工单不存在: {ticket_id}")
            actor_id = getattr(run_context, "user_id", None) or "vpn-agent"
            scopes = set(getattr(run_context, "scopes", frozenset()) or ())
            cmd = TicketCommand(
                ticket_id=ticket_id,
                action=action,
                actor_type=self._actor_for(action),
                actor_id=actor_id,
                expected_version=int(getattr(ticket, "version", 0) or 0),
                payload=payload or {},
            )
            transition_ticket(ticket.status, cmd, scopes=scopes)
            await tickets.transition(tenant_id, cmd, scopes=scopes)
            return True
        except (InvalidTicketTransition, TicketPermissionDenied) as exc:
            logger.info("VPN 状态迁移跳过（%s）: %s + %s", type(exc).__name__, ticket_id, action)
            return False
        except Exception as exc:  # noqa: BLE001  陈旧版本/其它异常视为非阻断
            logger.warning(
                "VPN 状态迁移失败 ticket=%s action=%s: %s", ticket_id, action, type(exc).__name__
            )
            return False

    @staticmethod
    def _actor_for(action: TicketAction) -> ActorType:
        if action == TicketAction.PROVIDE_ACTION_RESULT:
            return ActorType.CUSTOMER
        return ActorType.AGENT

    # ---- 内部：工单存在性校验 ----

    async def _ensure_ticket(self, runtime: Any, tenant_id: str, ticket_id: str) -> None:
        tickets = getattr(runtime, "tickets", None)
        if tickets is None or not hasattr(tickets, "get"):
            return
        ticket = await tickets.get(tenant_id, ticket_id)
        if ticket is None:
            raise VpnDiagnosisNotFound(f"工单不存在: {ticket_id}")

    # ---- 内部：审计 ----

    async def _audit(
        self,
        runtime: Any,
        run_context: Any,
        event_type: str,
        *,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        audit = getattr(runtime, "audit", None)
        if audit is None or run_context is None:
            return
        try:
            await audit.record_event(run_context, event_type, status=status, payload=payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("VPN 闭环审计写入失败 %s: %s", event_type, type(exc).__name__)

    # ---- 内部：PostgreSQL 持久化（方案 A，repository 存在时镜像写） ----

    async def _persist_run(self, run: VpnDiagnosisRun) -> None:
        if self.repository is None or not hasattr(self.repository, "save_run"):
            return
        try:
            await self.repository.save_run(run)
        except Exception as exc:  # noqa: BLE001  落库失败向上冒泡，禁止静默吞掉
            raise VpnPersistenceError(
                f"VPN 诊断运行落库失败 run={run.run_id}: {type(exc).__name__}"
            ) from exc

    async def _persist_action(self, action: VpnCustomerAction) -> None:
        if self.repository is None or not hasattr(self.repository, "add_action"):
            return
        try:
            await self.repository.add_action(action)
        except Exception as exc:  # noqa: BLE001
            raise VpnPersistenceError(
                f"VPN 排查步骤落库失败 action={action.action_id}: {type(exc).__name__}"
            ) from exc

    async def _persist_action_status(
        self, tenant_id: str, ticket_id: str, action_id: str, status: CustomerActionStatus
    ) -> None:
        if self.repository is None or not hasattr(self.repository, "update_action_status"):
            return
        try:
            await self.repository.update_action_status(tenant_id, ticket_id, action_id, status)
        except Exception as exc:  # noqa: BLE001
            raise VpnPersistenceError(
                f"VPN 排查步骤状态落库失败 action={action_id}: {type(exc).__name__}"
            ) from exc

    async def _persist_action_result(self, result: VpnCustomerActionResult) -> None:
        if self.repository is None or not hasattr(self.repository, "add_action_result"):
            return
        try:
            await self.repository.add_action_result(result)
        except Exception as exc:  # noqa: BLE001
            raise VpnPersistenceError(
                f"VPN 客户结果落库失败 result={result.result_id}: {type(exc).__name__}"
            ) from exc

    async def _persist_escalation(self, escalation: VpnEscalation) -> None:
        if self.repository is None or not hasattr(self.repository, "add_escalation"):
            return
        try:
            await self.repository.add_escalation(escalation)
        except Exception as exc:  # noqa: BLE001
            raise VpnPersistenceError(
                f"VPN 升级记录落库失败 esc={escalation.escalation_id}: {type(exc).__name__}"
            ) from exc

    # ---- 内部：工单时间线（append_status_event，诊断/派步骤/客户结果/升级进入时间线） ----

    async def _append_timeline(
        self,
        runtime: Any,
        run_context: Any,
        tenant_id: str,
        ticket_id: str,
        *,
        action: str,
        payload: dict[str, Any],
        actor_type: str | None = None,
    ) -> None:
        tickets = getattr(runtime, "tickets", None)
        if tickets is None or not hasattr(tickets, "append_status_event"):
            return
        try:
            actor_id = getattr(run_context, "user_id", None) or "vpn-agent"
            await tickets.append_status_event(
                tenant_id,
                ticket_id,
                action=action,
                actor_type=actor_type or "agent",
                actor_id=actor_id,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001  时间线写入失败不阻断主流程
            logger.warning(
                "VPN 时间线写入失败 ticket=%s action=%s: %s", ticket_id, action, type(exc).__name__
            )

    # ---- 内部：工具 ----

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError, OverflowError):
            return 0.0

    @staticmethod
    def _new_run_id() -> str:
        import uuid

        return f"diag_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _new_escalation_id() -> str:
        import uuid

        return f"esc_{uuid.uuid4().hex[:16]}"
