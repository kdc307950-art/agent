"""运行时装配 —— 把 Agent 图 + 所有基础设施组装成可用的 Runtime。

职责：
    - build_graph: 按 AGENT_GRAPH_MODE 构建图（single 单 Agent / workflow JSON 编排）
    - runtime_context: 应用生命周期内创建并持有 checkpointer(Postgres)、store、
      Redis 限流/撤销、工具治理、预算、审计等依赖的异步上下文管理器
"""

from __future__ import annotations

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
from .schema import check_schema_ready, ensure_schema_version
from .settings import Settings
from .tickets import (
    ItPolicyRepository,
    RoutingRepository,
    TicketOperationsRepository,
    TicketRepository,
)
from .tool_governance import ToolGovernance
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
    knowledge: KnowledgeRepository
    agentic_rag: AgenticRAGService | None
    tool_governance: ToolGovernance
    metrics: RuntimeMetrics
    copilot: CopilotService | None = None
    copilot_repository: CopilotRepository | None = None
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
            vector_retriever: Any = NullVectorRetriever()
            if settings.knowledge_embedding_endpoint and settings.knowledge_embedding_dimension:
                vector_retriever = PgVectorRetriever(
                    knowledge,
                    HttpEmbeddingProvider(
                        settings.knowledge_embedding_endpoint,
                        dimension=settings.knowledge_embedding_dimension,
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

            from src.my_agent.helpdesk.tools import RESOLUTION_COPILOT_TOOLS

            copilot_tools = {tool.name: tool for tool in RESOLUTION_COPILOT_TOOLS}
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
        yield AgentRuntime(
            graph=graph,
            intake_graph=build_helpdesk_intake_graph(
                checkpointer=checkpointer,
                rag_service=agentic_rag,
                it_policy_provider=ItPolicyRepository(audit.pool),
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
            knowledge=knowledge,
            agentic_rag=agentic_rag,
            tool_governance=tool_governance,
            metrics=runtime_metrics,
            copilot=copilot_service,
            copilot_repository=copilot_repository,
            graph_mode=settings.agent_graph_mode,
        )
