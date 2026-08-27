"""Helpdesk knowledge retrieval package."""

from .ingestion import DocumentEmbedder, IngestionPolicy, KnowledgeIngestionService
from .models import (
    Citation,
    KnowledgeChunkInput,
    KnowledgeDocumentInput,
    RetrievalHit,
    RetrievalPrincipal,
)
from .agentic import AgenticRAGPolicy, AgenticRAGService, RetrievalPlanner
from .pgvector import EmbeddingProvider, PgVectorRetriever
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
    "GeneratedAnswer",
    "GeneratedCitation",
    "IngestionPolicy",
    "KnowledgeAnswerService",
    "KnowledgeIngestionService",
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
