"""Knowledge persistence and retrieval models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    source_uri: str | None = Field(default=None, max_length=2_048)
    status: Literal["draft", "published", "retired"] = "draft"
    allowed_departments: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    ordinal: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=20_000)
    embedding_ref: str | None = Field(default=None, max_length=512)
    embedding_model: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    departments: frozenset[str] = frozenset()


class RetrievalHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    document_id: str
    document_version: int
    chunk_id: str
    title: str
    content: str
    source_uri: str | None
    source: Literal["lexical", "vector", "hybrid"]
    source_rank: int = Field(ge=1)
    fused_score: float = Field(default=0.0, ge=0.0)

    @property
    def key(self) -> tuple[str, int, str]:
        return self.document_id, self.document_version, self.chunk_id


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_version: int
    chunk_id: str
    title: str
    source_uri: str | None = None
