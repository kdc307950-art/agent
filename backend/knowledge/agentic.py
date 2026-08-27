"""Bounded Agentic RAG orchestration with deterministic safety gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .models import RetrievalHit, RetrievalPrincipal
from .service import AnswerDecision, KnowledgeAnswerService


class RetrievalPlanner(Protocol):
    async def next_queries(
        self,
        question: str,
        hits: Sequence[RetrievalHit],
        round_number: int,
    ) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class AgenticRAGPolicy:
    max_rounds: int = 3
    max_queries_per_round: int = 2
    max_contexts: int = 12
    allow_auto_reply: bool = False


class AgenticRAGService:
    """Let a planner refine retrieval, never bypassing KnowledgeAnswerService."""

    def __init__(
        self,
        answer_service: KnowledgeAnswerService,
        planner: RetrievalPlanner,
        *,
        policy: AgenticRAGPolicy | None = None,
    ) -> None:
        self.answer_service = answer_service
        self.planner = planner
        self.policy = policy or AgenticRAGPolicy()
        if self.policy.max_rounds < 1 or self.policy.max_queries_per_round < 1:
            raise ValueError("Agentic RAG policy 必须为正数")

    async def answer(
        self,
        principal: RetrievalPrincipal,
        question: str,
        *,
        category: str,
        risk_level: str,
        limit: int = 8,
    ) -> AnswerDecision:
        queries = [question]
        best: AnswerDecision | None = None
        all_hits: list[RetrievalHit] = []
        for round_number in range(self.policy.max_rounds):
            for query in queries[: self.policy.max_queries_per_round]:
                decision = await self.answer_service.answer(
                    principal, query, category=category, risk_level=risk_level, limit=limit
                )
                if decision.answer is not None:
                    best = decision
                if decision.auto_reply and self.policy.allow_auto_reply:
                    return decision
            # Planner receives only sanitized retrieval leaves, never tenant-wide objects.
            lexical = await self.answer_service.repository.lexical_search(principal, queries[0], limit=limit)
            all_hits.extend(lexical)
            if len(all_hits) >= self.policy.max_contexts:
                break
            next_queries = await self.planner.next_queries(question, all_hits, round_number)
            queries = [str(item).strip() for item in next_queries if str(item).strip()]
            if not queries:
                break
        if best is not None:
            reasons = tuple(dict.fromkeys((*best.reason_codes, "agentic_search_exhausted")))
            return best.model_copy(update={"auto_reply": False, "reason_codes": reasons})
        return AnswerDecision(
            answer=None,
            citations=(),
            auto_reply=False,
            reason_codes=("agentic_search_exhausted", "no_retrieval_hits"),
        )
