import asyncio

from backend.knowledge import (
    AgenticRAGPolicy,
    AgenticRAGService,
    GeneratedAnswer,
    GeneratedCitation,
    RetrievalHit,
    RetrievalPrincipal,
)


def make_hit(chunk_id, source):
    return RetrievalHit(
        tenant_id="tenant-a",
        document_id="doc-1",
        document_version=1,
        chunk_id=chunk_id,
        title="Runbook",
        content="safe context",
        source_uri="https://kb/doc-1",
        source=source,
        source_rank=1,
    )


class Repository:
    async def lexical_search(self, principal, query, *, limit):
        return [make_hit("chunk-1", "lexical")] if query == "refined" else []


class Vector:
    async def search(self, principal, query, *, limit):
        return [make_hit("chunk-1", "vector")] if query == "refined" else []


class Generator:
    async def generate(self, question, contexts):
        return GeneratedAnswer(
            text="Use the runbook.",
            citations=(
                GeneratedCitation(document_id="doc-1", document_version=1, chunk_id="chunk-1"),
            ),
        )


class Planner:
    def __init__(self):
        self.calls = []

    async def next_queries(self, question, hits, round_number):
        self.calls.append((question, round_number, len(hits)))
        return ["refined"] if round_number == 0 else []


def test_agentic_rag_is_bounded_and_never_upgrades_to_auto_reply_after_exhaustion():
    from backend.knowledge import KnowledgeAnswerService

    planner = Planner()
    service = AgenticRAGService(
        KnowledgeAnswerService(Repository(), Vector(), Generator()),
        planner,
        policy=AgenticRAGPolicy(max_rounds=2),
    )
    result = asyncio.run(
        service.answer(
            RetrievalPrincipal(tenant_id="tenant-a", departments={"it"}),
            "login issue",
            category="it",
            risk_level="low",
        )
    )

    assert result.answer == "Use the runbook."
    assert result.auto_reply is False
    assert "agentic_search_exhausted" in result.reason_codes
    assert planner.calls == [("login issue", 0, 0), ("login issue", 1, 1)]
