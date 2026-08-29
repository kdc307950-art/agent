"""Hybrid retrieval, citations, and explicit answer automation gates."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .models import Citation, RetrievalHit, RetrievalPrincipal
from .repository import KnowledgeRepository


class VectorRetriever(Protocol):
    async def search(
        self,
        principal: RetrievalPrincipal,
        query: str,
        *,
        limit: int,
    ) -> list[RetrievalHit]: ...


class AnswerGenerator(Protocol):
    async def generate(
        self,
        question: str,
        contexts: Sequence[RetrievalHit],
    ) -> GeneratedAnswer: ...


class GeneratedCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_version: int
    chunk_id: str


class GeneratedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=8_000)
    citations: tuple[GeneratedCitation, ...] = ()
    abstained: bool = False


class AnswerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str | None
    citations: tuple[Citation, ...]
    auto_reply: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerGatePolicy:
    minimum_hybrid_hits: int = 1
    require_both_retrievers: bool = True
    sensitive_categories: frozenset[str] = frozenset({"finance"})


class NullVectorRetriever:
    async def search(self, principal, query, *, limit):
        return []


def reciprocal_rank_fusion(
    lexical: Sequence[RetrievalHit],
    vector: Sequence[RetrievalHit],
    *,
    rank_constant: int = 60,
    limit: int = 10,
) -> list[RetrievalHit]:
    if rank_constant < 1 or limit < 1:
        raise ValueError("rank_constant 和 limit 必须为正数")
    by_key: dict[tuple[str, int, str], RetrievalHit] = {}
    scores: dict[tuple[str, int, str], float] = {}
    sources: dict[tuple[str, int, str], set[str]] = {}
    for source_name, hits in (("lexical", lexical), ("vector", vector)):
        for rank, hit in enumerate(hits, start=1):
            key = hit.key
            by_key.setdefault(key, hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank_constant + rank)
            sources.setdefault(key, set()).add(source_name)
    ordered = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    return [
        by_key[key].model_copy(
            update={
                "source": "hybrid" if len(sources[key]) > 1 else next(iter(sources[key])),
                "source_rank": index,
                "fused_score": scores[key],
            }
        )
        for index, key in enumerate(ordered, start=1)
    ]


class KnowledgeAnswerService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        vector_retriever: VectorRetriever,
        generator: AnswerGenerator,
        *,
        gate_policy: AnswerGatePolicy | None = None,
    ) -> None:
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
        lexical = await self.repository.lexical_search(principal, question, limit=limit)
        vector = await self.vector_retriever.search(principal, question, limit=limit)
        contexts = reciprocal_rank_fusion(lexical, vector, limit=limit)
        if not contexts:
            return AnswerDecision(
                answer=None,
                citations=(),
                auto_reply=False,
                reason_codes=("no_retrieval_hits",),
            )

        generated = await self.generator.generate(question, contexts)
        allowed = {hit.key: hit for hit in contexts}
        citations: list[Citation] = []
        invalid_citation = False
        for item in generated.citations:
            hit = allowed.get((item.document_id, item.document_version, item.chunk_id))
            if hit is None:
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

        reasons: list[str] = []
        hybrid_count = sum(hit.source == "hybrid" for hit in contexts)
        if generated.abstained:
            reasons.append("generator_abstained")
        if not citations:
            reasons.append("missing_citations")
        if invalid_citation:
            reasons.append("invalid_citation")
        if category in self.gate_policy.sensitive_categories or risk_level == "high":
            reasons.append("sensitive_or_high_risk")
        if (
            self.gate_policy.require_both_retrievers
            and hybrid_count < self.gate_policy.minimum_hybrid_hits
        ):
            reasons.append("insufficient_cross_retriever_support")

        auto_reply = not reasons
        return AnswerDecision(
            answer=None if generated.abstained else generated.text,
            citations=tuple(citations),
            auto_reply=auto_reply,
            reason_codes=tuple(reasons or ("gate_passed",)),
        )
