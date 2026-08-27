"""Helpdesk knowledge retrieval package."""

from .ingestion import DocumentEmbedder, IngestionPolicy, KnowledgeIngestionService
from .llm import LlmAnswerGenerator, LlmRetrievalPlanner
from .models import (
    Citation,
    KnowledgeChunkInput,
    KnowledgeDocumentInput,
    RetrievalHit,
    RetrievalPrincipal,
)
from .agentic import AgenticRAGPolicy, AgenticRAGService, RetrievalPlanner
from .pgvector import EmbeddingProvider, HttpEmbeddingProvider, PgVectorRetriever
from .repository import KnowledgeRepository
from .service import (
    AnswerDecision,
    AnswerGatePolicy,
    AnswerGenerator,
    GeneratedAnswer,
    GeneratedCitation,
    KnowledgeAnswerService,
    NullVectorRetriever,
    VectorRetriever,
    reciprocal_rank_fusion,
)

__all__ = [
    "AgenticRAGPolicy",
    "AgenticRAGService",
    "AnswerDecision",
    "AnswerGatePolicy",
    "AnswerGenerator",
    "Citation",
    "DocumentEmbedder",
    "EmbeddingProvider",
    "HttpEmbeddingProvider",
    "GeneratedAnswer",
    "GeneratedCitation",
    "IngestionPolicy",
    "KnowledgeAnswerService",
    "KnowledgeIngestionService",
    "LlmAnswerGenerator",
    "LlmRetrievalPlanner",
    "KnowledgeChunkInput",
    "KnowledgeDocumentInput",
    "KnowledgeRepository",
    "NullVectorRetriever",
    "PgVectorRetriever",
    "RetrievalHit",
    "RetrievalPlanner",
    "RetrievalPrincipal",
    "VectorRetriever",
    "reciprocal_rank_fusion",
]
