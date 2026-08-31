"""混合检索、引用校验与显式答案自动化门控（问答服务核心）。

职责：
    - KnowledgeAnswerService：编排 双路检索 -> RRF 融合 -> 生成 -> 引用过滤 -> 门控
    - reciprocal_rank_fusion：词法 + 向量结果的融合排序与跨路去重
    - AnswerGatePolicy / AnswerDecision：自动化应答的显式放行规则与决策记录

关键设计：
    - 所有"能否自动回复客户"的判断都收敛到 reason_codes：
      存在任一原因则 auto_reply=False，杜绝隐式放行
    - 模型引用必须先通过检索命中白名单校验，伪造 / 越界引用直接丢弃
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Citation, RetrievalHit, RetrievalPrincipal
from .repository import KnowledgeRepository


class VectorRetriever(Protocol):
    """向量检索器协议：按查询文本返回向量检索命中（ACL 由实现方保证）。"""

    async def search(
        self,
        principal: RetrievalPrincipal,
        query: str,
        *,
        limit: int,
    ) -> list[RetrievalHit]: ...


class AnswerGenerator(Protocol):
    """答案生成器协议：基于检索上下文生成结构化答案。"""

    async def generate(
        self,
        question: str,
        contexts: Sequence[RetrievalHit],
    ) -> GeneratedAnswer: ...


class GeneratedCitation(BaseModel):
    """模型原始输出的引用三元组（尚未经过白名单校验）。

    校验发生在 KnowledgeAnswerService.answer：引用键必须落在
    融合后的检索命中集合里才被采纳，否则视为非法引用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_version: int
    chunk_id: str


class GeneratedAnswer(BaseModel):
    """生成器的结构化输出：答案文本 + 原始引用 + 是否放弃作答。

    abstained=True 表示模型没有把握（或生成失败），此时 text 为空，
    服务层会把最终 answer 置为 None 并拒绝自动回复。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(default="", max_length=8_000)
    citations: tuple[GeneratedCitation, ...] = ()
    abstained: bool = False

    @model_validator(mode="after")
    def require_text_unless_abstained(self) -> GeneratedAnswer:
        if not self.abstained and not self.text.strip():
            raise ValueError("非 abstained 答案必须包含文本")
        return self


class AnswerDecision(BaseModel):
    """一次问答的最终决策（对上层完全可解释）。

    answer: 答案文本；None 表示拒绝作答（abstained 或未检索到内容）
    citations: 通过白名单校验的最终引用（补充了标题与来源）
    auto_reply: 是否允许自动回复客户；False 时必须有 reason_codes 说明原因
    reason_codes: 门控原因的稳定编码，供上层记录 / 上报；空则记 "gate_passed"
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str | None
    citations: tuple[Citation, ...]
    auto_reply: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerGatePolicy:
    """自动化应答门控策略（不可变）。

    minimum_hybrid_hits: 双路同时命中的最低条数
    require_both_retrievers: 是否要求词法 + 向量双路印证——
                             仅单路命中的内容可能漏检，默认不放行自动回复
    sensitive_categories: 敏感类别集合，命中即禁止自动回复
                          （默认含 finance 财务类）
    """

    minimum_hybrid_hits: int = 1
    require_both_retrievers: bool = True
    sensitive_categories: frozenset[str] = frozenset({"finance"})

    def __post_init__(self) -> None:
        if self.minimum_hybrid_hits < 0:
            raise ValueError("minimum_hybrid_hits 不能为负数")


class NullVectorRetriever:
    """空向量检索器：总是返回空结果。

    用于未配置向量检索能力的降级场景——服务层仍然走词法检索，
    但 require_both_retrievers 会阻止自动回复，行为可预测。
    """

    async def search(self, principal, query, *, limit):
        return []


def reciprocal_rank_fusion(
    lexical: Sequence[RetrievalHit],
    vector: Sequence[RetrievalHit],
    *,
    rank_constant: int = 60,
    limit: int = 10,
) -> list[RetrievalHit]:
    """对词法与向量两路结果做倒数排名融合（RRF）并去重。

    参数：
        lexical / vector: 两路检索命中（各自已按来源内排名排好）
        rank_constant: RRF 平滑常数 k，控制排名靠前结果的权重（默认 60）
        limit: 融合后保留的条数
    返回：
        融合排序的命中列表；同键（文档+版本+分块）只保留一条，
        source 标记为 "hybrid"（双路命中）或原单路来源
    设计：
        - 分数 = Σ 1/(k + rank)：不依赖两路分数尺度可比，
          只依赖各自的相对排名，天然免疫分数分布差异
        - 用 hit.key 做跨路去重，同一分块被两路同时命中时融合分更高
    """
    if rank_constant < 1 or limit < 1:
        raise ValueError("rank_constant 和 limit 必须为正数")
    # key -> 原始命中 / 累积分数 / 命中的来源集合，三个字典并行维护
    by_key: dict[tuple[str, int, str], RetrievalHit] = {}
    scores: dict[tuple[str, int, str], float] = {}
    sources: dict[tuple[str, int, str], set[str]] = {}
    for source_name, hits in (("lexical", lexical), ("vector", vector)):
        for rank, hit in enumerate(hits, start=1):
            key = hit.key
            # 先到先得保留首个命中对象（两路同一分块内容一致，取哪个都行）
            by_key.setdefault(key, hit)
            # RRF 核心公式：排名越靠前，1/(k+rank) 贡献越大
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank_constant + rank)
            sources.setdefault(key, set()).add(source_name)
    # 按融合分降序、键升序（保证同分时顺序确定），截断到 limit
    ordered = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    return [
        by_key[key].model_copy(
            update={
                # 双路命中标记 hybrid；单路保留原来源名
                "source": "hybrid" if len(sources[key]) > 1 else next(iter(sources[key])),
                "source_rank": index,
                "fused_score": scores[key],
            }
        )
        for index, key in enumerate(ordered, start=1)
    ]


class KnowledgeAnswerService:
    """问答服务：检索 -> 融合 -> 生成 -> 引用校验 -> 门控 的完整编排。

    所有协作者都走协议 / 抽象（repository / vector_retriever / generator），
    便于替换实现与单元测试；门控逻辑是这里唯一不可替换的核心。
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        vector_retriever: VectorRetriever,
        generator: AnswerGenerator,
        *,
        gate_policy: AnswerGatePolicy | None = None,
    ) -> None:
        """构造问答服务。

        参数：
            repository: 知识仓库（提供词法检索）
            vector_retriever: 向量检索器（协议；可传 NullVectorRetriever 降级）
            generator: 答案生成器（协议）
            gate_policy: 自动回复门控策略；为 None 时用默认策略
        """
        self.repository = repository
        self.vector_retriever = vector_retriever
        self.generator = generator
        self.gate_policy = gate_policy or AnswerGatePolicy()

    async def answer(
        self,
        principal: RetrievalPrincipal,
        question: str,
        *,
        category: str,
        risk_level: str,
        limit: int = 8,
    ) -> AnswerDecision:
        """执行一次完整问答，返回可解释的决策（含是否允许自动回复）。

        参数：
            principal: 检索主体（ACL 由两路检索器共同执行）
            question: 用户问题
            category / risk_level: 门控上下文（敏感类别 / 高风险场景禁自动回复）
            limit: 每路检索的条数上限，也作为融合后的条数
        返回：
            AnswerDecision；无检索结果时直接返回
            ("no_retrieval_hits",) 的拒绝决策，不调用生成器。
        """
        if not question or not question.strip():
            raise ValueError("question 不能为空")
        if limit < 1 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        # 双路检索：词法 + 向量，随后 RRF 融合去重
        lexical = await self.repository.lexical_search(principal, question, limit=limit)
        vector = await self.vector_retriever.search(principal, question, limit=limit)
        contexts = reciprocal_rank_fusion(lexical, vector, limit=limit)
        if not contexts:
            # 没有任何可引用上下文：拒绝作答且不允许自动回复
            return AnswerDecision(
                answer=None,
                citations=(),
                auto_reply=False,
                reason_codes=("no_retrieval_hits",),
            )

        generated = await self.generator.generate(question, contexts)
        # 引用白名单：只接受出现在检索上下文中的 (文档, 版本, 分块)
        allowed = {hit.key: hit for hit in contexts}
        citations: list[Citation] = []
        invalid_citation = False
        for item in generated.citations:
            hit = allowed.get((item.document_id, item.document_version, item.chunk_id))
            if hit is None:
                # 模型引用了上下文之外的内容：标记为非法引用并丢弃
                invalid_citation = True
                continue
            citations.append(
                Citation(
                    document_id=hit.document_id,
                    document_version=hit.document_version,
                    chunk_id=hit.chunk_id,
                    title=hit.title,
                    source_uri=hit.source_uri,
                )
            )

        # 门控原因收集：存在任一原因 => 禁止自动回复（显式优于隐式）
        reasons: list[str] = []
        hybrid_count = sum(hit.source == "hybrid" for hit in contexts)
        if generated.abstained:
            reasons.append("generator_abstained")
        if not citations:
            reasons.append("missing_citations")
        if invalid_citation:
            reasons.append("invalid_citation")
        # 敏感类别或高风险：宁可人工确认，绝不自动回复
        if category in self.gate_policy.sensitive_categories or risk_level == "high":
            reasons.append("sensitive_or_high_risk")
        # 要求双路印证但融合后 hybrid 命中不足：单路证据不足，不放行
        if (
            self.gate_policy.require_both_retrievers
            and hybrid_count < self.gate_policy.minimum_hybrid_hits
        ):
            reasons.append("insufficient_cross_retriever_support")

        # 没有任何门控原因时才允许自动回复
        auto_reply = not reasons
        return AnswerDecision(
            answer=None if generated.abstained else generated.text,
            citations=tuple(citations),
            auto_reply=auto_reply,
            # 空原因记 "gate_passed"，保证 reason_codes 永不为空，便于上层日志/统计
            reason_codes=tuple(reasons or ("gate_passed",)),
        )
