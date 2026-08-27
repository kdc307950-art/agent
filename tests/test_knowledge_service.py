import asyncio

from backend.knowledge import (
    AnswerGatePolicy,
    GeneratedAnswer,
    GeneratedCitation,
    KnowledgeAnswerService,
    RetrievalHit,
    RetrievalPrincipal,
    reciprocal_rank_fusion,
)


def hit(chunk_id: str, *, source: str, rank: int, tenant: str = "tenant-a") -> RetrievalHit:
    return RetrievalHit(
        tenant_id=tenant,
        document_id="doc-1",
        document_version=2,
        chunk_id=chunk_id,
        title="SSO Runbook",
        content=f"content {chunk_id}",
        source_uri="https://kb.example/doc-1",
        source=source,
        source_rank=rank,
    )


def test_rrf_uses_rank_and_merges_duplicate_chunks_without_raw_score_comparison():
    lexical = [hit("a", source="lexical", rank=1), hit("b", source="lexical", rank=2)]
    vector = [hit("b", source="vector", rank=1), hit("c", source="vector", rank=2)]

    fused = reciprocal_rank_fusion(lexical, vector, rank_constant=10)

    assert [item.chunk_id for item in fused] == ["b", "a", "c"]
    assert fused[0].source == "hybrid"
    assert fused[0].fused_score > fused[1].fused_score
    assert [item.source_rank for item in fused] == [1, 2, 3]


class FakeRepository:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def lexical_search(self, principal, query, *, limit):
        self.calls.append((principal, query, limit))
        return self.hits


class FakeVector:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def search(self, principal, query, *, limit):
        self.calls.append((principal, query, limit))
        return self.hits


class FakeGenerator:
    def __init__(self, answer):
        self.answer = answer
        self.contexts = None

    async def generate(self, question, contexts):
        self.contexts = contexts
        return self.answer


def run_answer(repository_hits, vector_hits, generated, *, category="it", risk="low"):
    repository = FakeRepository(repository_hits)
    vector = FakeVector(vector_hits)
    generator = FakeGenerator(generated)
    service = KnowledgeAnswerService(repository, vector, generator)
    principal = RetrievalPrincipal(tenant_id="tenant-a", departments={"it"})
    result = asyncio.run(
        service.answer(principal, "How to reset SSO?", category=category, risk_level=risk)
    )
    return result, repository, vector, generator


def test_answer_gate_passes_only_with_cross_retriever_support_and_valid_citation():
    generated = GeneratedAnswer(
        text="Reset the SSO session.",
        citations=(GeneratedCitation(document_id="doc-1", document_version=2, chunk_id="a"),),
    )
    result, repository, vector, generator = run_answer(
        [hit("a", source="lexical", rank=1)],
        [hit("a", source="vector", rank=1)],
        generated,
    )

    assert result.auto_reply is True
    assert result.reason_codes == ("gate_passed",)
    assert result.citations[0].chunk_id == "a"
    assert repository.calls[0][0].tenant_id == "tenant-a"
    assert repository.calls[0][0].departments == frozenset({"it"})
    assert generator.contexts[0].source == "hybrid"


def test_answer_gate_rejects_fabricated_citation():
    generated = GeneratedAnswer(
        text="Do something unsupported.",
        citations=(GeneratedCitation(document_id="doc-x", document_version=1, chunk_id="fake"),),
    )
    result, *_ = run_answer(
        [hit("a", source="lexical", rank=1)],
        [hit("a", source="vector", rank=1)],
        generated,
    )

    assert result.auto_reply is False
    assert result.citations == ()
    assert set(result.reason_codes) == {"missing_citations", "invalid_citation"}


def test_sensitive_or_high_risk_answer_is_never_auto_replied():
    generated = GeneratedAnswer(
        text="Finance instructions.",
        citations=(GeneratedCitation(document_id="doc-1", document_version=2, chunk_id="a"),),
    )
    finance, *_ = run_answer(
        [hit("a", source="lexical", rank=1)],
        [hit("a", source="vector", rank=1)],
        generated,
        category="finance",
    )
    high_risk, *_ = run_answer(
        [hit("a", source="lexical", rank=1)],
        [hit("a", source="vector", rank=1)],
        generated,
        risk="high",
    )

    assert finance.auto_reply is False
    assert high_risk.auto_reply is False
    assert "sensitive_or_high_risk" in finance.reason_codes


def test_single_retriever_support_and_no_hits_do_not_auto_reply():
    generated = GeneratedAnswer(
        text="Possible answer.",
        citations=(GeneratedCitation(document_id="doc-1", document_version=2, chunk_id="a"),),
    )
    single, *_ = run_answer([hit("a", source="lexical", rank=1)], [], generated)
    no_hits, _repository, _vector, generator = run_answer([], [], generated)

    assert single.auto_reply is False
    assert "insufficient_cross_retriever_support" in single.reason_codes
    assert no_hits.answer is None
    assert no_hits.reason_codes == ("no_retrieval_hits",)
    assert generator.contexts is None


def test_gate_policy_can_require_more_cross_retriever_evidence():
    repository = FakeRepository([hit("a", source="lexical", rank=1)])
    vector = FakeVector([hit("a", source="vector", rank=1)])
    generator = FakeGenerator(
        GeneratedAnswer(
            text="Answer",
            citations=(GeneratedCitation(document_id="doc-1", document_version=2, chunk_id="a"),),
        )
    )
    service = KnowledgeAnswerService(
        repository,
        vector,
        generator,
        gate_policy=AnswerGatePolicy(minimum_hybrid_hits=2),
    )
    result = asyncio.run(
        service.answer(
            RetrievalPrincipal(tenant_id="tenant-a", departments={"it"}),
            "question",
            category="it",
            risk_level="low",
        )
    )

    assert result.auto_reply is False
    assert result.reason_codes == ("insufficient_cross_retriever_support",)
