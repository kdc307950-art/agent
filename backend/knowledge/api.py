"""Knowledge document management API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.security import Principal, rate_limit_dependency

from .models import KnowledgeChunkInput, KnowledgeDocumentInput
from .repository import KnowledgeRepository


router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class CreateKnowledgeDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: KnowledgeDocumentInput
    chunks: list[KnowledgeChunkInput] = Field(min_length=1, max_length=500)


class PublishKnowledgeDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


def _runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not hasattr(runtime, "knowledge"):
        raise HTTPException(status_code=503, detail="知识库服务尚未初始化")
    return runtime


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"缺少 {scope} 权限")


@router.post("/documents", status_code=201)
async def create_knowledge_document(
    payload: CreateKnowledgeDocumentRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "knowledge:write")
    runtime = _runtime(request)
    repository: KnowledgeRepository = runtime.knowledge
    await repository.put_document(
        principal.tenant_id,
        payload.document,
        payload.chunks,
    )
    return {"document_id": payload.document.document_id, "version": payload.document.version}


@router.post("/documents/{document_id}/publish")
async def publish_knowledge_document(
    document_id: str,
    payload: PublishKnowledgeDocumentRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "knowledge:write")
    runtime = _runtime(request)
    repository: KnowledgeRepository = runtime.knowledge
    try:
        await repository.publish_document_version(principal.tenant_id, document_id, payload.version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"document_id": document_id, "version": payload.version, "status": "published"}
