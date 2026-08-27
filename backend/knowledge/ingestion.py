"""Deterministic knowledge ingestion pipeline for Agentic RAG."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from .models import KnowledgeChunkInput, KnowledgeDocumentInput, RetrievalPrincipal
from .pgvector import PgVectorRetriever
from .repository import KnowledgeRepository


class DocumentEmbedder(Protocol):
    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True, slots=True)
class IngestionPolicy:
    chunk_chars: int = 1200
    overlap_chars: int = 160


class KnowledgeIngestionService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        vector: PgVectorRetriever,
        embedder: DocumentEmbedder,
        *,
        embedding_model: str,
        policy: IngestionPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.vector = vector
        self.embedder = embedder
        self.embedding_model = embedding_model
        self.policy = policy or IngestionPolicy()
        if self.policy.chunk_chars < 200 or not 0 <= self.policy.overlap_chars < self.policy.chunk_chars:
            raise ValueError("知识切片参数无效")

    def clean_text(self, text: str) -> str:
        text = text.replace("\x00", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def chunk_text(self, text: str) -> list[KnowledgeChunkInput]:
        cleaned = self.clean_text(text)
        if not cleaned:
            raise ValueError("文档正文为空")
        step = self.policy.chunk_chars - self.policy.overlap_chars
        chunks = []
        for ordinal, start in enumerate(range(0, len(cleaned), step)):
            content = cleaned[start : start + self.policy.chunk_chars]
            if not content:
                break
            digest = hashlib.sha256(content.encode()).hexdigest()[:16]
            chunks.append(
                KnowledgeChunkInput(
                    chunk_id=f"c{ordinal:05d}-{digest}",
                    ordinal=ordinal,
                    content=content,
                    embedding_model=self.embedding_model,
                )
            )
            if start + self.policy.chunk_chars >= len(cleaned):
                break
        return chunks

    async def ingest(
        self,
        tenant_id: str,
        document: KnowledgeDocumentInput,
        text: str,
    ) -> int:
        chunks = self.chunk_text(text)
        embeddings = await self.embedder.embed_documents([chunk.content for chunk in chunks])
        if len(embeddings) != len(chunks):
            raise RuntimeError("embedding 数量与切片数量不匹配")
        target_status = document.status
        await self.repository.put_document(
            tenant_id,
            document.model_copy(update={"status": "draft"}),
            chunks,
        )
        principal = RetrievalPrincipal(tenant_id=tenant_id, departments=set(document.allowed_departments))
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            await self.vector.put_embedding(
                principal,
                document_id=document.document_id,
                document_version=document.version,
                chunk_id=chunk.chunk_id,
                embedding=embedding,
                embedding_model=self.embedding_model,
            )
        if target_status == "published":
            await self.repository.publish_document_version(
                tenant_id, document.document_id, document.version
            )
        return len(chunks)
