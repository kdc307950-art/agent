"""LANGGraph VPN Diagnosis Agent — 服务编排：组装上下文 -> 执行 Agent -> 诊断门禁。

模块归属：backend/vpn。设计要点（对齐 docs/product/vpn-diagnosis-agent-contract.md）：
    - VpnDiagnosisService.prepare_context：读取工单/文本/错误码/资产，判定
      多用户是否受影响（fault==multi_user_impact）与身份是否完整（RunContext 有 tenant_id/user_id）。
    - VpnDiagnosisService.run_with_context：组装 DiagnosisRequest -> 执行
      VpnDiagnosisAgent -> apply_handoff（evaluate_handoff 四类必须转人工）-> 产出 DiagnosisCommand。
    - 恒不自动发客户消息、不改工单：本服务只诊断、只产出处置命令，执行交给 executor 业务层。
    - 不持有 runtime 引用（避免装配阶段循环依赖）：所有方法在执行时显式接收 runtime 参数。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .agent import VpnDiagnosisAgent
from .models import DiagnosisRequest, evaluate_handoff
from .rules import (
    AccountStatus,
    GatewayStatus,
    VpnEvidence,
    evaluate_evidence,
    normalize_account_status,
    normalize_gateway_status,
)

logger = logging.getLogger("langgraph.vpn")


def _identity_ok(run_context: Any) -> bool:
    """判定身份是否完整：RunContext 必须同时具备 tenant_id 与 user_id。

    身份缺失（无 tenant_id / user_id）属于契约四类必须转人工场景之一。
    """
    if run_context is None:
        return False
    tenant_id = getattr(run_context, "tenant_id", None)
    user_id = getattr(run_context, "user_id", None)
    return bool(tenant_id) and bool(user_id)


class VpnDiagnosisService:
    """VPN 诊断编排服务：上下文准备 + 生成 + 诊断门禁。

    只做只读诊断与命令产出，不落任何副作用；实际命令执行由 executor 完成。
    """

    def __init__(self, agent: VpnDiagnosisAgent) -> None:
        self.agent = agent

    async def prepare_context(
        self,
        *,
        runtime,
        tenant_id: str,
        ticket_id: str,
        run_context=None,
    ) -> DiagnosisRequest:
        """读取工单只读上下文，组装 DiagnosisRequest。

        只读数据源：工单记录 + 概览；fault 来自工单分类（若概览已给出则优先），
        否则回退为 connection_failed。identity_ok 由 run_context 判定。
        """
        tickets = runtime.tickets
        ticket = await tickets.get(tenant_id, ticket_id)
        if ticket is None:
            raise LookupError("工单不存在")
        overview: dict[str, Any] = {}
        try:
            overview = await runtime.ticket_operations.get_ticket_overview(tenant_id, ticket_id)
        except Exception as exc:
            logger.warning("读取工单概览失败 ticket=%s: %s", ticket_id, type(exc).__name__)

        intake = overview.get("intake") or {}
        fault = str(intake.get("fault") or "") if intake.get("fault") else ""
        if not fault:
            # 概览未带 fault 时，用本地确定性关键词判定或回退连接失败
            from src.my_agent.helpdesk import classify_vpn_fault

            fault = classify_vpn_fault(f"{ticket.title}\n{ticket.description}")
        if not fault:
            fault = "connection_failed"

        messages = overview.get("messages") or []
        message_text = "\n".join(
            f"[{m.get('direction')}] {m.get('actor_id')}: {m.get('content')}"
            for m in messages[-12:]
        )
        text = f"{ticket.title}\n{ticket.description}\n历史消息：\n{message_text or '（无）'}"

        return DiagnosisRequest(
            ticket_id=ticket.ticket_id,
            requester_id=ticket.requester_id,
            tenant_id=tenant_id,
            ticket_text=text,
            fault=fault,
            asset_id=ticket.asset_id,
            current_status=ticket.status.value,
            identity_ok=_identity_ok(run_context),
        )

    async def diagnose(
        self,
        request: DiagnosisRequest,
        runtime=None,
        *,
        run_context=None,
    ) -> dict[str, Any]:
        """执行 VPN 诊断 Agent，返回原始结果（未门禁，含结构化证据与命令）。"""
        return await self.agent.run(request, runtime=runtime, run_context=run_context)

    @staticmethod
    def apply_handoff(raw: dict[str, Any], request: DiagnosisRequest) -> dict[str, Any]:
        """独立诊断门禁（evaluate_handoff 四类必须转人工）+ 证据链结构化假设落定。

        对 Agent 输出做最终安全裁定：
            - error_code 非空 -> 强制 must_handoff=True
            - command 为 None -> 强制 must_handoff=True
            - 否则复用 agent 侧 evaluate_handoff 判定（multi_user/identity/no_evidence/low_confidence）
        同时把工具证据归一化为 VpnEvidence，调用 rules.evaluate_evidence 得到结构化
        EvidenceDiagnosis（hypothesis/confidence/evidence/ruled_out/next_action/reason_codes），
        写入 result["evidence_chain"]，供 VpnDiagnosisRun 落库与 M3 真实口径消费。
        恒不自动发客户消息、不改工单（本层只做裁定）。
        """
        if not isinstance(raw, dict):
            raw = {}
        raw_error = raw.get("error_code")
        error_code = str(raw_error)[:64] if raw_error else None
        command = raw.get("command")
        must_handoff = bool(raw.get("must_handoff"))
        evaluation = raw.get("evaluation")

        if error_code or command is None:
            must_handoff = True
        if not isinstance(evaluation, dict) or not evaluation:
            # 防御：无判定结果时重新计算（不信任 agent 侧标记）
            evaluation = evaluate_handoff(
                evidence=raw.get("tool_evidence") or [],
                fault=request.fault,
                confidence=_command_confidence(command),
                identity_ok=request.identity_ok,
            ).model_dump(mode="json")
            must_handoff = bool(evaluation.get("must_handoff"))

        result = dict(raw)
        result["must_handoff"] = must_handoff
        result["evaluation"] = evaluation
        result["request"] = request.model_dump(mode="json")

        # 阶段三：证据链结构化假设（M3 真实口径）。把工具证据归一化为 VpnEvidence，
        # 调用 rules.evaluate_evidence 得到结构化 hypothesis。用 rules 优先级覆盖
        # agent 侧猜测，保证评测拿到确定性的 hypothesis/evidence/ruled_out/confidence。
        result["evidence_chain"] = _evidence_chain_diagnosis(raw, request)
        return result

    async def run_with_context(
        self,
        *,
        runtime,
        tenant_id: str,
        ticket_id: str,
        run_context=None,
    ) -> dict[str, Any]:
        """带租户上下文执行完整流程：准备上下文 -> 生成 -> 诊断门禁。

        返回 {"request", "raw", "result"}；result 为已过门禁的处置结果。
        """
        request = await self.prepare_context(
            runtime=runtime, tenant_id=tenant_id, ticket_id=ticket_id, run_context=run_context
        )
        raw = await self.diagnose(request, runtime=runtime, run_context=run_context)
        if not isinstance(raw, dict):
            raw = {"error_code": "invalid_agent_result"}
        result = self.apply_handoff(raw, request)
        return {"request": request, "raw": raw, "result": result}


def _command_confidence(command: Any) -> float:
    """从 command dict 提取 confidence，非法回退 0.0（保证低置信度触发转人工）。"""
    if not isinstance(command, dict):
        return 0.0
    try:
        value = float(command.get("confidence") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, min(1.0, value))


def _evidence_chain_diagnosis(raw: dict[str, Any], request: DiagnosisRequest) -> dict[str, Any]:
    """把 Agent 工具证据归一化为 VpnEvidence 并产出证据链结构化假设（M3 真实口径）。

    从 tool_evidence（工具返回的依据）/ tool_trace 提取账号/网关/知识/多用户信号，
    归一化为 VpnEvidence，调用 rules.evaluate_evidence 得到确定性 EvidenceDiagnosis。
    返回 JSON 兼容 dict（hypothesis/confidence/evidence/ruled_out/next_action/reason_codes/
    must_handoff），供 VpnDiagnosisRun 落库与 M3 fault_hypothesis 消费。
    """
    tool_evidence = raw.get("tool_evidence") or []
    evidence_hint = raw.get("evidence_chain") or {}
    account = AccountStatus.UNKNOWN
    gateway = GatewayStatus.UNKNOWN
    knowledge_hit = False
    has_asset = False
    client_version = ""
    required_version = ""
    error_code = ""
    multi_user = request.fault == "multi_user_impact"

    for item in tool_evidence:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or ""
        tool_name = str(item.get("tool_name") or "")
        text = str(content).casefold()
        if "account" in tool_name or ("账号" in text and "status" in text):
            account = normalize_account_status(_status_from_content(content))
        elif "gateway" in tool_name or "网关" in text:
            gateway = normalize_gateway_status(_status_from_content(content))
        elif "config_version" in tool_name or "client_version" in tool_name:
            client_version = str(content or "") or client_version
        elif "knowledge" in tool_name or "知识库" in text:
            knowledge_hit = True
        elif "asset" in tool_name:
            has_asset = True

    evidence = VpnEvidence(
        fault=request.fault,
        account_status=account,
        gateway_status=gateway,
        client_version=client_version,
        required_version=required_version,
        error_code=error_code,
        multi_user_impact=multi_user,
        identity_ok=request.identity_ok,
        knowledge_hit=knowledge_hit or bool(evidence_hint),
        has_asset=has_asset,
    )
    diagnosis = evaluate_evidence(evidence)
    return diagnosis.to_dict()


_STATUS_KW_RE = re.compile(r"(active|locked|disabled|expired|up|degraded|down)", re.IGNORECASE)


def _status_from_content(content: str) -> dict[str, str]:
    """从工具返回的展示文本中提取状态关键词，返回 ``{"status": ...}`` 供信号归一化。"""
    match = _STATUS_KW_RE.search(str(content or "").casefold())
    return {"status": match.group(0).lower() if match else "unknown"}
