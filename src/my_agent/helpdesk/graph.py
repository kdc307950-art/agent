"""工单受理 LangGraph：归一化 → 分类 → 策略 → 完整性 → 澄清/派单 → 拟答。

职责：
    - 把客户提交的工单文本/字段跑完「受理全流程」：
      归一化字段 → 分类（关键词/模型）→ 应用租户 IT 策略 → 检查必填字段
      → 缺信息时 interrupt 追问客户 → 派单（团队/优先级/风险）→ 可选 RAG 拟答
    - 通过 LangGraph checkpointer 支持中断恢复（clarify 节点 interrupt）
    - 可选 vpn_diagnose 桥接节点：对 it.vpn + 字段齐 + 服务注入的受理结果，
      调用只读 VPN Diagnosis Agent 产出处置命令；仅把非升级命令交给业务校验，
      must_handoff（no_evidence/low_confidence/multi_user_impact/identity_missing）
      时不执行任何命令、不自动回复，仅标记转人工/升级并写审计。

关键设计：
    - it_policy_provider 运行时按 tenant_id 动态查询策略，不在启动时预编译，
      支持租户策略热更新；无策略时回退内置 IntakePolicy 默认行为
    - clarify 节点用 interrupt 挂起等待客户补充，resume 后从同一 checkpoint 继续
    - compose_answer 可选：注入 rag_service 后才启用拟答，否则静默跳过
    - vpn_diagnose 仅在注入 vpn_diagnosis 服务时加入图；否则完全惰性（状态机仍是外层控制器）
"""

from __future__ import annotations

from time import monotonic
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from backend.run_context import RunContext
from backend.tool_governance import VPN_DIAGNOSIS_TOOLS

from .domain import ActorType, ResumeAction
from .intake import (
    IntakePolicy,
    KeywordTicketClassifier,
    TicketCategory,
    TicketClassifier,
    assess_and_dispatch,
    clarification_question,
    classify_vpn_fault,
    normalize_fields,
)


class HelpdeskIntakeState(TypedDict, total=False):
    """受理图的共享状态：字段/文本、分类结果、策略应用结果、派单决策、拟答。

    身份与范围（Day 4）：tenant_id / user_id / departments / asset_id 由
    认证主体或渠道入站事件经 config 注入；缺失时收紧权限并转人工。
    """

    ticket_id: str
    requester_id: str
    text: str
    fields: dict[str, Any]
    category: str
    subcategory: str
    classification_signals: list[str]
    classification_needs_review: bool
    classification_confidence: float
    missing_fields: list[str]
    clarification_rounds: int
    clarification_exhausted: bool
    dispatch_team_id: str
    priority: str
    risk_level: str
    dispatch_reason_codes: list[str]
    # 身份上下文（服务端注入，前端/请求体不可直接提交）
    user_id: str
    departments: list[str]
    asset_id: str
    identity_missing: bool
    # 渠道身份目录无映射（Day 3-4）：空部门/空资产且转人工
    channel_identity_missing: bool
    # ItPolicyProvider 应用结果（动态租户策略，非启动时预编译）
    policy_category: str | None
    policy_required_fields: list[str]
    policy_priority: str | None
    approval_required: bool
    auto_answer_enabled: bool
    draft_answer: str | None
    citations: list[dict[str, Any]]
    auto_reply: bool
    answer_reason_codes: list[str]
    answer_status: str
    # VPN Diagnosis Agent 桥接结果（可选；仅当注入 vpn_diagnosis 服务且命中
    # it.vpn + 字段齐时填充）。状态机仍是外层控制器：这里只读诊断并落标记，
    # 不直接改工单、不自动回复、不调用副作用动作。
    vpn_diagnosis_run: bool
    vpn_diagnosis_run_id: str | None
    vpn_diagnosis_must_handoff: bool
    vpn_diagnosis_handoff_reasons: list[str]
    vpn_diagnosis_command: dict[str, Any] | None
    vpn_diagnosis_evaluation: dict[str, Any] | None
    vpn_diagnosis_error_code: str | None
    vpn_diagnosis_next_action: str | None
    next: str


def build_helpdesk_intake_graph(
    *,
    classifier: TicketClassifier | None = None,
    policy: IntakePolicy | None = None,
    checkpointer=None,
    rag_service=None,
    it_policy_provider=None,
    vpn_diagnosis=None,
    runtime_provider=None,
):
    """构建受理图。

    it_policy_provider：async get(tenant_id, category) -> TenantItPolicy | None。
    分类完成后按当前 tenant_id 动态查询（it.vpn -> 回退 it -> 默认），
    不在启动时为每个租户预编译图。
    """
    classifier = classifier or KeywordTicketClassifier()
    policy = policy or IntakePolicy()

    def normalize_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """字段归一化：合并/清洗客户字段，补入 requester_id。"""
        fields = normalize_fields(state.get("fields") or {})
        if state.get("requester_id") and not fields.get("requester_id"):
            fields["requester_id"] = state["requester_id"]
        return {"fields": fields}

    async def classify_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """文本分类：产出 category/subcategory/置信度/是否需要人工复核。"""
        result = await classifier.classify(state.get("text", ""), state.get("fields") or {})
        return {
            "category": result.category.value,
            "subcategory": result.subcategory,
            "classification_signals": list(result.signals),
            "classification_needs_review": result.needs_human_review,
            "classification_confidence": result.confidence,
        }

    async def apply_policy_node(
        state: HelpdeskIntakeState, config: RunnableConfig
    ) -> dict[str, Any]:
        """按分类链（子分类 -> 父分类）动态加载租户 IT 策略并写入状态。

        无策略时返回全默认（不强制必填、不要求审批、不自动回答）。
        """
        tenant_id = str((config.get("configurable") or {}).get("tenant_id") or "")
        if not tenant_id or it_policy_provider is None:
            return {
                "policy_category": None,
                "policy_required_fields": [],
                "policy_priority": None,
                "approval_required": False,
                "auto_answer_enabled": False,
            }
        category = state.get("category", "other")
        subcategory = state.get("subcategory") or "general"
        candidates = (
            [category] if subcategory == "general" else [f"{category}.{subcategory}", category]
        )
        found = None
        for key in candidates:
            found = await it_policy_provider.get(tenant_id, key)
            if found is not None:
                break
        if found is None:
            return {
                "policy_category": None,
                "policy_required_fields": [],
                "policy_priority": None,
                "approval_required": False,
                "auto_answer_enabled": False,
            }
        return {
            "policy_category": found.category,
            "policy_required_fields": list(found.required_fields),
            "policy_priority": found.default_priority,
            "approval_required": found.approval_required,
            "auto_answer_enabled": found.auto_answer_enabled,
        }

    def completeness_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """检查必填字段（内置分类要求 ∪ 策略额外要求）；缺失则进入 clarify。"""
        category = TicketCategory(state.get("category", TicketCategory.OTHER.value))
        extra = frozenset(state.get("policy_required_fields") or [])
        fields = state.get("fields") or {}
        missing = tuple(
            sorted(
                name
                for name in (policy.required_fields(category) | extra)
                if fields.get(name) in (None, "", [], {})
            )
        )
        rounds = int(state.get("clarification_rounds", 0))
        exhausted = bool(missing) and rounds >= policy.clarification_limit(category)
        return {
            "missing_fields": list(missing),
            "clarification_exhausted": exhausted,
            "next": "dispatch" if not missing or exhausted else "clarify",
        }

    def clarify_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """追问缺失字段：interrupt 挂起等待客户补充（resume 后从 checkpoint 继续）。

        对响应做三重校验：动作必须是 provide_information、参与者必须是客户、
        提交人必须等于 requester_id，防止他人代填。
        """
        rounds = int(state.get("clarification_rounds", 0))
        response = interrupt(
            {
                "kind": "ticket_clarification",
                "ticket_id": state["ticket_id"],
                "expected_actor": ActorType.CUSTOMER.value,
                "expected_actor_id": state.get("requester_id"),
                "allowed_actions": [ResumeAction.PROVIDE_INFORMATION.value],
                "question": clarification_question(state.get("missing_fields") or []),
            }
        )
        if (
            not isinstance(response, dict)
            or response.get("action") != ResumeAction.PROVIDE_INFORMATION.value
        ):
            raise ValueError("补充信息恢复命令无效")
        if response.get("actor_type") != ActorType.CUSTOMER.value:
            raise ValueError("补充信息必须由客户提交")
        expected_actor_id = state.get("requester_id")
        if expected_actor_id and response.get("actor_id") != expected_actor_id:
            raise ValueError("补充信息提交人不匹配")
        payload = response.get("payload") or {}
        supplied = payload.get("fields") if isinstance(payload, dict) else None
        if not isinstance(supplied, dict):
            raise ValueError("补充信息必须包含 payload.fields")
        fields = dict(state.get("fields") or {})
        fields.update(normalize_fields(supplied))
        return {
            "fields": fields,
            "clarification_rounds": rounds + 1,
        }

    def dispatch_node(state: HelpdeskIntakeState) -> dict[str, Any]:
        """派单决策：团队 + 优先级 + 风险等级 + 原因码（叠加策略优先级/审批要求）。"""
        category = TicketCategory(state.get("category", TicketCategory.OTHER.value))
        decision = assess_and_dispatch(
            text=state.get("text", ""),
            category=category,
            classification_needs_review=bool(state.get("classification_needs_review", False)),
            clarification_exhausted=bool(state.get("clarification_exhausted", False)),
            policy=policy,
        )
        priority = state.get("policy_priority") or decision.priority
        reason_codes = list(decision.reason_codes)
        if state.get("approval_required"):
            reason_codes.append("approval_required")
        identity_missing = bool(state.get("channel_identity_missing", False))
        if identity_missing:
            reason_codes.append("channel_identity_missing")
        target_team = decision.team_id
        if identity_missing:
            # 渠道身份目录无映射：即便分类为 IT 也转服务台人工队列
            target_team = policy.team_by_category[TicketCategory.OTHER]
        return {
            "dispatch_team_id": target_team,
            "priority": priority,
            "risk_level": decision.risk_level.value,
            "dispatch_reason_codes": reason_codes,
            "next": "finish",
        }

    async def compose_answer_node(
        state: HelpdeskIntakeState, config: RunnableConfig
    ) -> dict[str, Any]:
        """可选：调用 RAG 服务生成拟答与引用；未注入 rag_service 时跳过。

        主体构造（Day 4）：tenant_id / user_id / departments / asset_id 均从
        config（认证主体或渠道入站事件注入）读取，不使用空部门集；
        身份缺失（无 tenant_id 或 user_id）时收紧权限并转人工：
          - 检索主体 departments 保持空集，internal 由认证主体/渠道身份目录决定
          - answer_status=handoff_high_risk，auto_reply=False，
            answer_reason_codes 追加 identity_missing
        """
        if rag_service is None:
            return {}
        from backend.knowledge import RetrievalPrincipal
        from backend.knowledge.service import answer_status

        configurable = config.get("configurable") or {}
        tenant_id = str(configurable.get("tenant_id") or "")
        user_id = str(configurable.get("user_id") or "")
        raw_departments = configurable.get("departments") or []
        departments = (
            frozenset(str(item) for item in raw_departments)
            if isinstance(raw_departments, (list, tuple, set, frozenset))
            else frozenset()
        )
        identity_missing = bool(state.get("channel_identity_missing", False)) or not bool(
            tenant_id and user_id
        )
        # Day 4：internal 不再固定 False，来自认证主体/渠道身份目录
        principal = RetrievalPrincipal(
            tenant_id=tenant_id or "unknown",
            departments=departments,
            internal=bool(configurable.get("internal", False)),
        )
        decision = await rag_service.answer(
            principal,
            state.get("text", ""),
            category=state.get("category", "other"),
            risk_level=state.get("risk_level", "low"),
        )
        status = answer_status(decision.reason_codes, auto_reply=decision.auto_reply)
        reason_codes = list(decision.reason_codes)
        if identity_missing:
            # 身份缺失：不默认全库/全部门权限，必须转人工
            status = "handoff_high_risk"
            reason_codes.append("identity_missing")
        return {
            "draft_answer": decision.answer,
            "citations": [item.model_dump(mode="json") for item in decision.citations],
            "auto_reply": decision.auto_reply and not identity_missing,
            "answer_reason_codes": reason_codes,
            "answer_status": status,
            "identity_missing": identity_missing,
        }

    async def _run_closed_loop(
        state: HelpdeskIntakeState,
        config: RunnableConfig,
        runtime: Any,
        closed_loop: Any,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """阶段二闭环：调用 VpnClosedLoopService.diagnose 落 VpnDiagnosisRun + 派步骤 + 状态机联动。

        返回的 vpn_diagnosis_* 字段供 compose_answer / 时间线消费。闭环失败/无服务时优雅
        返回转人工标记，不中断受理主流程（与只读诊断的失败语义一致）。
        """
        ticket_id = state.get("ticket_id", "")
        run_context = RunContext(
            run_id=f"vpn-closed-{ticket_id}",
            request_id=f"vpn-closed-{ticket_id}",
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=f"vpn:{tenant_id}:{ticket_id}",
            scopes=frozenset({"ticket:agent"}),
            deadline=monotonic() + 15.0,
            allowed_tools=VPN_DIAGNOSIS_TOOLS,
        )
        try:
            outcome: dict[str, Any] = await closed_loop.diagnose(
                runtime=runtime, tenant_id=tenant_id, ticket_id=ticket_id, run_context=run_context
            )
        except Exception as exc:  # noqa: BLE001  闭环失败不冒泡，回退为转人工标记
            _ = type(exc).__name__  # 留痕（不中断受理主流程）
            return {
                "vpn_diagnosis_run": True,
                "vpn_diagnosis_must_handoff": True,
                "vpn_diagnosis_handoff_reasons": ["diagnosis_failed"],
                "vpn_diagnosis_command": None,
                "vpn_diagnosis_evaluation": {
                    "must_handoff": True,
                    "handoff_reasons": ["diagnosis_failed"],
                },
                "vpn_diagnosis_error_code": "vpn_closed_loop_failed",
            }
        run = outcome.get("run") or {} if isinstance(outcome, dict) else {}
        result = outcome.get("result") or {} if isinstance(outcome, dict) else {}
        dispatch = outcome.get("dispatch") or {} if isinstance(outcome, dict) else {}
        must_handoff = bool(result.get("must_handoff")) or str(dispatch.get("status")) == "handed_off"
        return {
            "vpn_diagnosis_run": True,
            "vpn_diagnosis_run_id": run.get("run_id"),
            "vpn_diagnosis_must_handoff": must_handoff,
            "vpn_diagnosis_handoff_reasons": list(
                (result.get("evaluation") or {}).get("handoff_reasons", [])
            ),
            "vpn_diagnosis_command": result.get("command"),
            "vpn_diagnosis_evaluation": result.get("evaluation"),
            "vpn_diagnosis_error_code": result.get("error_code"),
            "vpn_diagnosis_next_action": run.get("next_action"),
        }

    async def vpn_diagnose_node(
        state: HelpdeskIntakeState, config: RunnableConfig
    ) -> dict[str, Any]:
        """可选桥接节点：对 it.vpn + 字段齐的受理结果调用只读 VPN Diagnosis Agent。

        状态机仍是外层控制器：
            - 仅在注入 vpn_diagnosis 服务、分类为 it.vpn、字段齐全、有租户身份时触发；
            - 调用 runtime.vpn_diagnosis 执行只读诊断，取回 DiagnosisCommand；
            - 若 must_handoff（no_evidence/low_confidence/multi_user_impact/identity_missing）
              则不执行任何命令、不自动回复，仅标记转人工/升级并写审计；
            - 否则只把命令交给业务层校验（DiagnosisCommandValidator），禁止命令直接拒绝，
              本节点绝不直接改工单 / 发消息 / 关单 / 重启网关等副作用。
        """
        if vpn_diagnosis is None:
            return {"vpn_diagnosis_run": False}
        # 惰性导入：backend.vpn 会反向导入 src.my_agent.helpdesk（经 executor 导入 domain），
        # 若在模块顶层导入会造成循环。仅当需要构造 DiagnosisRequest / 校验命令时按需导入。
        from backend.vpn import DiagnosisCommand, DiagnosisCommandValidator, DiagnosisRequest

        category = state.get("category")
        subcategory = state.get("subcategory") or "general"
        if not (category == "it" and subcategory == "vpn"):
            return {"vpn_diagnosis_run": False}
        if state.get("missing_fields"):
            # 字段不齐：交由状态机追问，不触发诊断（不改变既有分支语义）。
            return {"vpn_diagnosis_run": False}
        configurable = config.get("configurable") or {}
        tenant_id = str(configurable.get("tenant_id") or "")
        user_id = str(configurable.get("user_id") or "")
        if not tenant_id:
            return {
                "vpn_diagnosis_run": False,
                "vpn_diagnosis_must_handoff": True,
                "vpn_diagnosis_handoff_reasons": ["identity_missing"],
            }
        runtime = runtime_provider() if callable(runtime_provider) else None
        if runtime is None:
            return {"vpn_diagnosis_run": False}

        # 阶段二闭环：若运行时的 vpn_closed_loop（VpnClosedLoopService）已装配，
        # 则走「诊断落库 + 状态机联动 + 给客户步骤/升级」闭环，而非仅只读诊断。
        closed_loop = getattr(runtime, "vpn_closed_loop", None)
        if closed_loop is not None:
            return await _run_closed_loop(
                state, config, runtime, closed_loop, tenant_id, user_id
            )

        text = state.get("text", "")
        fault = classify_vpn_fault(text) if text else "connection_failed"
        run_context = RunContext(
            run_id=f"vpn-{state.get('ticket_id', '')}",
            request_id=f"vpn-{state.get('ticket_id', '')}",
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=f"vpn:{tenant_id}:{state.get('ticket_id', '')}",
            scopes=frozenset({"ticket:agent"}),
            deadline=monotonic() + 15.0,
            allowed_tools=VPN_DIAGNOSIS_TOOLS,
        )
        request = DiagnosisRequest(
            ticket_id=state.get("ticket_id", ""),
            requester_id=state.get("requester_id", ""),
            tenant_id=tenant_id,
            ticket_text=text,
            fault=fault,
            asset_id=state.get("asset_id") or None,
            current_status="",
            identity_ok=bool(tenant_id and user_id),
        )
        try:
            raw = await vpn_diagnosis.diagnose(request, runtime=runtime, run_context=run_context)
            result = vpn_diagnosis.apply_handoff(raw, request)
        except Exception as exc:
            result = {
                "must_handoff": True,
                "error_code": "vpn_diagnosis_failed",
                "command": None,
                "evaluation": {
                    "must_handoff": True,
                    "handoff_reasons": [],
                    "evidence_found": False,
                    "confidence": 0.0,
                },
                "tool_trace": [],
                "tool_evidence": [],
            }
            _ = type(exc).__name__  # 留痕（不向上冒泡，保证受理主流程不中断）

        must_handoff = bool(result.get("must_handoff"))
        command_payload = result.get("command")
        evaluation = result.get("evaluation")
        error_code = result.get("error_code")

        # 非升级命令只有通过业务层校验才标记为可执行；升级/禁止命令一律拦截。
        validated_command = None
        if not must_handoff and isinstance(command_payload, dict):
            try:
                command = DiagnosisCommand.model_validate(command_payload)
                DiagnosisCommandValidator().validate(command)
                validated_command = command.model_dump(mode="json")
            except Exception:
                # 禁止/非法命令：本身已被模型校验或业务校验拒绝，降级为转人工。
                must_handoff = True
                handoff_reasons = list(
                    result.get("evaluation", {}).get("handoff_reasons", [])
                )
                if "forbidden_command" not in handoff_reasons:
                    handoff_reasons.append("forbidden_command")
                result = {
                    **result,
                    "must_handoff": True,
                    "evaluation": {
                        **(result.get("evaluation") or {}),
                        "must_handoff": True,
                        "handoff_reasons": handoff_reasons,
                    },
                }
                evaluation = result.get("evaluation")

        # 写审计（转人工/升级）——只读诊断不产生任何工单副作用。
        if must_handoff:
            audit = getattr(runtime, "audit", None)
            if audit is not None and run_context is not None:
                try:
                    await audit.record_event(
                        run_context,
                        "vpn_diagnosis_handoff",
                        status="handoff",
                        payload={
                            "handoff_reasons": list(
                                (evaluation or {}).get("handoff_reasons", [])
                            ),
                            "command": command_payload.get("command")
                            if isinstance(command_payload, dict)
                            else None,
                        },
                    )
                except Exception:
                    pass

        return {
            "vpn_diagnosis_run": True,
            "vpn_diagnosis_must_handoff": must_handoff,
            "vpn_diagnosis_handoff_reasons": list(
                (evaluation or {}).get("handoff_reasons", [])
            ),
            "vpn_diagnosis_command": validated_command,
            "vpn_diagnosis_evaluation": evaluation,
            "vpn_diagnosis_error_code": str(error_code) if error_code else None,
        }

    def route_after_completeness(state: HelpdeskIntakeState) -> str:
        return state["next"]

    graph = StateGraph(HelpdeskIntakeState)
    graph.add_node("normalize", normalize_node)
    graph.add_node("classify", classify_node)
    graph.add_node("apply_policy", apply_policy_node)
    graph.add_node("check_completeness", completeness_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("dispatch", dispatch_node)
    graph.add_node("compose_answer", compose_answer_node)
    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "classify")
    graph.add_edge("classify", "apply_policy")
    graph.add_edge("apply_policy", "check_completeness")
    # 字段齐全 -> dispatch；缺失且未耗尽追问次数 -> clarify（interrupt 等客户补充）
    graph.add_conditional_edges(
        "check_completeness",
        route_after_completeness,
        {"clarify": "clarify", "dispatch": "dispatch"},
    )
    graph.add_edge("clarify", "check_completeness")
    if vpn_diagnosis is None:
        # 未注入 VPN 服务：维持原有 dispatch -> compose_answer（兼容既有测试）。
        graph.add_edge("dispatch", "compose_answer")
    else:
        # 注入 VPN 服务：dispatch -> vpn_diagnose -> compose_answer，
        # vpn_diagnose_node 对非 it.vpn/字段不齐/无服务的路径是惰性透传（vpn_diagnosis_run=False），
        # 不改变既有分支语义；命中 it.vpn + 字段齐时才做只读诊断并落转人工标记。
        graph.add_node("vpn_diagnose", vpn_diagnose_node)
        graph.add_edge("dispatch", "vpn_diagnose")
        graph.add_edge("vpn_diagnose", "compose_answer")
    graph.add_edge("compose_answer", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())
