"""运行时装配 —— 把 Agent 图 + 所有基础设施组装成可用的 Runtime。

职责：
    - build_graph: 按 AGENT_GRAPH_MODE 构建图（single 单 Agent / workflow JSON 编排）
    - runtime_context: 应用生命周期内创建并持有 checkpointer(Postgres)、store、
      Redis 限流/撤销、工具治理、预算、审计等依赖的异步上下文管理器
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg import AsyncConnection

from src.my_agent.agent import build_agent
from src.my_agent.helpdesk import build_helpdesk_intake_graph
from src.my_agent.workflow import build_workflow_from_json

from .assets import AssetRepository
from .audit import AuditRepository, audit_context
from .channel_identities import ChannelIdentityRepository
from .copilot.agent import ResolutionCopilot
from .copilot.repository import CopilotRepository
from .copilot.service import CopilotService
from .knowledge import (
    AgenticRAGPolicy,
    AgenticRAGService,
    AnswerGatePolicy,
    HttpEmbeddingProvider,
    KnowledgeAnswerService,
    KnowledgeRepository,
    LlmAnswerGenerator,
    LlmRetrievalPlanner,
    NullVectorRetriever,
    PgVectorRetriever,
)
from .metrics import RuntimeMetrics
from .repositories import LongTermMemoryRepository
from .run_context import RunContext
from .schema import check_schema_ready, ensure_schema_version
from .settings import Settings
from .tickets import (
    ItPolicyRepository,
    RoutingRepository,
    TicketOperationsRepository,
    TicketRepository,
)
from .tool_governance import ToolGovernance
from .vpn.closed_loop import VpnClosedLoopService
from .vpn.reissue_service import VpnReissueService
from .vpn.repository import VpnDiagnosisRepository
from .vpn.service import VpnDiagnosisService
from .workflow_loader import load_workflow_spec


@dataclass
class AgentRuntime:
    graph: object
    intake_graph: object
    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore
    memory: LongTermMemoryRepository
    audit: AuditRepository
    tickets: TicketRepository
    ticket_operations: TicketOperationsRepository
    routing: RoutingRepository
    assets: AssetRepository
    it_policies: ItPolicyRepository
    channel_identities: ChannelIdentityRepository
    knowledge: KnowledgeRepository
    agentic_rag: AgenticRAGService | None
    tool_governance: ToolGovernance
    metrics: RuntimeMetrics
    knowledge_retriever: object | None = None
    copilot: CopilotService | None = None
    copilot_repository: CopilotRepository | None = None
    # VPN Diagnosis Agent（只读诊断组件；未配置模型 key 时 None，API 返回 503）。
    vpn_diagnosis: VpnDiagnosisService | None = None
    # VPN 客户处置闭环（阶段二）：在只读诊断之上落库 + 状态机联动 + 客户动作闭环。
    vpn_closed_loop: VpnClosedLoopService | None = None
    # VPN 处置闭环 PostgreSQL 持久化仓储（方案 A，写 vpn_* 表；未配置模型 key 时 None）。
    vpn_diagnosis_repo: VpnDiagnosisRepository | None = None
    # 只读 VPN 适配器（Mock/真实，供工具经 runtime.vpn_adapter 访问）。
    vpn_adapter: object | None = None
    # 重新下发 VPN 配置的审批式执行编排服务（依赖 audit/tickets/vpn_adapter，运行时取）。
    vpn_reissue: VpnReissueService | None = None
    graph_mode: str = "single"


def build_graph(
    settings: Settings,
    *,
    checkpointer,
    store,
    tool_governance: ToolGovernance,
    rag_service: AgenticRAGService | None = None,
):
    """按 AGENT_GRAPH_MODE 构建生产图。

    - single：单 Agent（默认，历史行为）
    - workflow：由 JSON 编排定义编译，支持 supervisor 路由与 human_approval 审批

    两种形态共用同一套 checkpointer / store / 工具治理钩子，因此多租户隔离、
    审计、预算、限流对上层完全一致；差异只在图结构本身。
    """
    if settings.agent_graph_mode == "workflow":
        # spec 不合法时在此直接抛错，不让服务带病启动
        spec = load_workflow_spec(settings)
        return build_workflow_from_json(
            spec,
            checkpointer=checkpointer,
            store=store,
            api_key=settings.deepseek_api_key,
            base_url=settings.llm_base_url,
            model_name=settings.llm_model,
            tool_call_wrapper=tool_governance.awrap_tool_call,
            rag_service=rag_service,
        )
    return build_agent(
        checkpointer=checkpointer,
        store=store,
        model_retry_attempts=settings.model_retry_attempts,
        api_key=settings.deepseek_api_key,
        base_url=settings.llm_base_url,
        model_name=settings.llm_model,
        tool_call_wrapper=tool_governance.awrap_tool_call,
    )


@asynccontextmanager
async def runtime_context(
    settings: Settings,
    *,
    metrics: RuntimeMetrics | None = None,
) -> AsyncIterator[AgentRuntime]:
    async with AsyncExitStack() as stack:
        checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.database_url)
        )
        store = await stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(settings.database_url)
        )
        audit = await stack.enter_async_context(audit_context(settings.database_url))
        if settings.auto_setup:
            await checkpointer.setup()
            await store.setup()
            await audit.setup()
        async with await AsyncConnection.connect(settings.database_url) as connection:
            if settings.auto_setup:
                await ensure_schema_version(connection)
            await check_schema_ready(connection)
        await audit.check_ready()
        runtime_metrics = metrics or RuntimeMetrics()
        tool_governance = ToolGovernance(
            audit,
            tenant_allowlist=settings.tool_tenant_allowlist,
            max_retry_attempts=settings.tool_retry_attempts,
            metrics=runtime_metrics,
        )
        knowledge = KnowledgeRepository(audit.pool)
        agentic_rag: AgenticRAGService | None = None
        vector_retriever: Any = NullVectorRetriever()  # 块外初始化：未配置 embedding 时保持 Null
        if settings.deepseek_api_key:
            generator = LlmAnswerGenerator(
                api_key=settings.deepseek_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
            )
            planner = LlmRetrievalPlanner(
                api_key=settings.deepseek_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
            )
            if settings.knowledge_embedding_endpoint and settings.knowledge_embedding_dimension:
                vector_retriever = PgVectorRetriever(
                    knowledge,
                    HttpEmbeddingProvider(
                        settings.knowledge_embedding_endpoint,
                        dimension=settings.knowledge_embedding_dimension,
                        auth_token=settings.knowledge_embedding_token,
                        model=settings.knowledge_embedding_model,
                    ),
                    dimension=settings.knowledge_embedding_dimension,
                )
            answer_service = KnowledgeAnswerService(
                knowledge,
                vector_retriever,
                generator,
                gate_policy=AnswerGatePolicy(
                    require_both_retrievers=True, sensitive_categories=frozenset({"finance"})
                ),
            )
            agentic_rag = AgenticRAGService(
                answer_service,
                planner,
                policy=AgenticRAGPolicy(allow_auto_reply=False),
            )
        graph = build_graph(
            settings,
            checkpointer=checkpointer,
            store=store,
            tool_governance=tool_governance,
            rag_service=agentic_rag,
        )
        # Resolution Copilot：有界只读工具循环（解决阶段分析与拟答）。
        # 仅当配置了真实模型 key 时才启用；否则 copilot 为 None（API 返回 503）。
        copilot_repository = CopilotRepository(audit.pool)
        copilot_service: CopilotService | None = None
        if settings.deepseek_api_key:
            from langchain_openai import ChatOpenAI
            from pydantic import SecretStr

            from .copilot.tools import COPILOT_TOOLS

            copilot_tools = {tool.name: tool for tool in COPILOT_TOOLS}
            copilot_model = ChatOpenAI(
                api_key=SecretStr(settings.deepseek_api_key),
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                temperature=0,
            )
            copilot_service = CopilotService(
                ResolutionCopilot(
                    model=copilot_model,
                    tools=copilot_tools,
                )
            )
        # 统一知识检索入口（阶段二）：Copilot search_knowledge 经此执行。
        # 未配置 embedding 时 vector_retriever 为 NullVectorRetriever，
        # KnowledgeRetriever 自动标记 lexical-only；配置后 hybrid。
        from .knowledge.retriever import KnowledgeRetriever

        knowledge_retriever = KnowledgeRetriever(knowledge, vector_retriever)
        # VPN Diagnosis Agent：只读诊断组件。仅当配置了真实模型 key 时才启用，
        # 否则 vpn_diagnosis / vpn_adapter 为 None（API 返回 503）。构造方式与
        # copilot 完全对称：MockVpnAdapter(数据可来自 vpn_mock_data_path) +
        # ChatOpenAI(temperature=0) + VpnDiagnosisAgent(全经 governed_invoke 走治理)。
        vpn_adapter: object | None = None
        vpn_diagnosis_service: VpnDiagnosisService | None = None
        vpn_closed_loop_service: VpnClosedLoopService | None = None
        vpn_diagnosis_repo: VpnDiagnosisRepository | None = None
        if settings.deepseek_api_key:
            from langchain_openai import ChatOpenAI
            from pydantic import SecretStr

            from .vpn.agent import VpnDiagnosisAgent
            from .vpn.sandbox_adapter import build_vpn_adapter
            from .vpn.service import VpnDiagnosisService
            from .vpn.tools import VPN_TOOLS

            # 阶段四：数据源模式通过 VPN_ADAPTER_MODE 切换（mock=固定Mock / sandbox=可重复沙箱）。
            # 只读能力统一经 VpnResilientAdapter 韧性包装（request_id/timeout/retry/熔断/权限/审计/脱敏）。
            vpn_adapter = build_vpn_adapter(
                settings.vpn_adapter_mode,
                seed=settings.vpn_sandbox_seed,
                data_path=settings.vpn_mock_data_path if settings.vpn_mock_data_path else None,
            )
            vpn_tools = {tool.name: tool for tool in VPN_TOOLS}
            vpn_model = ChatOpenAI(
                api_key=SecretStr(settings.deepseek_api_key),
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                temperature=0,
            )
            vpn_diagnosis_service = VpnDiagnosisService(
                VpnDiagnosisAgent(
                    model=vpn_model,
                    tools=vpn_tools,
                )
            )
            # VPN 客户处置闭环（阶段二）：复用 vpn_diagnosis 服务编排，叠加落库 + 状态机联动。
            # PostgreSQL 持久化（方案 A）：把诊断运行/排查步骤/客户结果/升级镜像写入 vpn_* 表。
            vpn_diagnosis_repo = VpnDiagnosisRepository(audit.pool)
            vpn_closed_loop_service = VpnClosedLoopService(
                diagnosis_service=vpn_diagnosis_service,
                repository=vpn_diagnosis_repo,
            )
        # runtime_provider：图节点（vpn_diagnose）需要在执行期访问运行中的
        # AgentRuntime（拿到 vpn_diagnosis 服务与 audit）。由于 intake_graph 在
        # AgentRuntime 构造时构建，用可变 holder 在构造完成后回填，避免环形硬引用。
        runtime_holder: dict[str, Any] = {}


        def _runtime_provider() -> Any:
            return runtime_holder.get("self")


        runtime = AgentRuntime(
            graph=graph,
            intake_graph=build_helpdesk_intake_graph(
                checkpointer=checkpointer,
                rag_service=agentic_rag,
                it_policy_provider=ItPolicyRepository(audit.pool),
                vpn_diagnosis=vpn_diagnosis_service,
                runtime_provider=_runtime_provider,
            ),
            checkpointer=checkpointer,
            store=store,
            memory=LongTermMemoryRepository(store),
            audit=audit,
            tickets=TicketRepository(audit.pool),
            ticket_operations=TicketOperationsRepository(audit.pool),
            routing=RoutingRepository(audit.pool),
            assets=AssetRepository(audit.pool),
            it_policies=ItPolicyRepository(audit.pool),
            channel_identities=ChannelIdentityRepository(audit.pool),
            knowledge=knowledge,
            agentic_rag=agentic_rag,
            tool_governance=tool_governance,
            metrics=runtime_metrics,
            knowledge_retriever=knowledge_retriever,
            copilot=copilot_service,
            copilot_repository=copilot_repository,
            vpn_diagnosis=vpn_diagnosis_service,
            vpn_closed_loop=vpn_closed_loop_service,
            vpn_diagnosis_repo=vpn_diagnosis_repo,
            vpn_adapter=vpn_adapter,
            vpn_reissue=VpnReissueService() if vpn_diagnosis_service else None,
            graph_mode=settings.agent_graph_mode,
        )
        runtime_holder["self"] = runtime
        yield runtime


async def reconcile_pending_reissues(runtime: AgentRuntime) -> list[dict[str, Any]]:
    """补偿对账 worker 入口：扫描 vpn_reissue 注册表中 execution_unknown / reconciliation_required
    的幂等键，逐键向外部系统重查真实结果并收敛到定态（补写 workflow_operation + 工单 + 审计）。

    供未来后台 worker / 定时任务调用；生产环境可在 ReissueRegistry 之上叠加 workflow_operation
    持久化后，改由数据库扫描触发。此处提供内存版扫描，保证「状态不依赖单次请求完成」可验证。
    """
    svc = runtime.vpn_reissue
    if svc is None:
        return []
    results: list[dict[str, Any]] = []
    for key in svc.registry.scan_reconcilable():
        req_dict = svc.registry.get_request(key)
        if not req_dict:
            continue
        ctx = RunContext(
            run_id=f"vpn-reconcile-{uuid.uuid4().hex[:12]}",
            request_id=uuid.uuid4().hex[:12],
            tenant_id=str(req_dict.get("tenant_id") or ""),
            user_id="reconciliation-worker",
            thread_id=f"vpn:{req_dict.get('tenant_id')}:{req_dict.get('ticket_id') or '-'}",
            scopes=frozenset({"ticket:agent", "ticket:system"}),
            deadline=time.time() + 60,
            allowed_tools=None,
        )
        results.append(
            await svc.reconcile(idempotency_key=key, runtime=runtime, run_context=ctx)
        )
    return results
