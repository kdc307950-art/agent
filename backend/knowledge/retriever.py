"""统一知识检索入口 —— KnowledgeRetriever（阶段三）。

职责：
    - 封装 lexical_search + vector_search + RRF 融合 + ACL/有效期过滤为单一入口
    - 按是否配置 embedding 决定检索策略：
        lexical-only（jieba + PostgreSQL 全文 + pg_trgm）
        hybrid（lexical 候选 + vector 候选 + RRF 融合）
    - 返回带 retrieval_mode 标记的命中，供 Copilot/问答服务复用

关键设计：
    - 与 KnowledgeAnswerService 的融合逻辑一致（reciprocal_rank_fusion），
      避免多入口行为分叉；本类是无状态门面，可注入任意 VectorRetriever
    - retrieval_mode 显式标记（lexical-only / hybrid），测评与文档据此
      分开记录，不把两套结果混为一个指标
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import RetrievalHit, RetrievalPrincipal
from .repository import KnowledgeRepository
from .service import NullVectorRetriever, VectorRetriever, reciprocal_rank_fusion

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalResult:
    """一次检索的完整结果（含模式标记与每路命中）。"""

    hits: list[RetrievalHit]
    retrieval_mode: str  # "lexical-only" | "hybrid"
    lexical_hits: list[RetrievalHit] = field(default_factory=list)
    vector_hits: list[RetrievalHit] = field(default_factory=list)
    hybrid_hits: int = 0


class KnowledgeRetriever:
    """统一检索门面：lexical/vector/RRF + 模式标记。

    参数：
        repository: 知识仓库（lexical_search / verify_citations）
        vector_retriever: 向量检索器；传 NullVectorRetriever 表示未配置
                          embedding（降级 lexical-only）
        fusion_limit: RRF 融合后保留的条数
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        vector_retriever: VectorRetriever | None = None,
        *,
        fusion_limit: int = 10,
    ) -> None:
        self.repository = repository
        self.vector_retriever = vector_retriever or NullVectorRetriever()
        self.fusion_limit = fusion_limit
        # 是否真实配置了向量检索（NullVectorRetriever 视为未配置）
        self.vector_enabled = not isinstance(self.vector_retriever, NullVectorRetriever)

    @property
    def retrieval_mode(self) -> str:
        return "hybrid" if self.vector_enabled else "lexical-only"

    async def search(
        self,
        principal: RetrievalPrincipal,
        query: str,
        *,
        limit: int = 10,
    ) -> KnowledgeRetrievalResult:
        """执行一次统一检索，返回带模式标记的结果。

        未配置 embedding：仅 lexical（标记 lexical-only）；
        配置 embedding：lexical + vector 双路 + RRF 融合（标记 hybrid）。
        """
        lexical = await self.repository.lexical_search(principal, query, limit=limit)
        if not self.vector_enabled:
            return KnowledgeRetrievalResult(
                hits=lexical[:limit],
                retrieval_mode="lexical-only",
                lexical_hits=lexical,
                vector_hits=[],
            )
        try:
            vector = await self.vector_retriever.search(principal, query, limit=limit)
        except Exception as exc:
            # 向量检索失败降级为 lexical-only（不阻断主流程），但记录日志
            logger.warning("向量检索失败，降级 lexical-only: %s", exc)
            return KnowledgeRetrievalResult(
                hits=lexical[:limit],
                retrieval_mode="lexical-only",
                lexical_hits=lexical,
                vector_hits=[],
            )
        fused = reciprocal_rank_fusion(lexical, vector, limit=self.fusion_limit)
        hybrid_count = sum(h.source == "hybrid" for h in fused)
        return KnowledgeRetrievalResult(
            hits=fused[:limit],
            retrieval_mode="hybrid",
            lexical_hits=lexical,
            vector_hits=vector,
            hybrid_hits=hybrid_count,
        )
