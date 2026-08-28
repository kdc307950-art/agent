"""Knowledge document management API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


async def _audit(runtime, *, tenant_id: str, user_id: str, action: str, resource_id: str) -> None:
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    await audit.record_admin_event(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource_type="knowledge_document",
        resource_id=resource_id,
    )


@router.get("/documents")
async def list_knowledge_documents(
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _require_scope(principal, "knowledge:read")
    runtime = _runtime(request)
    repository: KnowledgeRepository = runtime.knowledge
    items = await repository.list_documents(principal.tenant_id, limit=limit, offset=offset)
    return {"items": items}


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
    await _audit(runtime, tenant_id=principal.tenant_id, user_id=principal.user_id, action="knowledge.document.create", resource_id=payload.document.document_id)
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
    await _audit(runtime, tenant_id=principal.tenant_id, user_id=principal.user_id, action="knowledge.document.publish", resource_id=document_id)
    return {"document_id": document_id, "version": payload.version, "status": "published"}


@router.post("/documents/{document_id}/retire")
async def retire_knowledge_document(
    document_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "knowledge:write")
    runtime = _runtime(request)
    repository: KnowledgeRepository = runtime.knowledge
    retired = await repository.retire_document(principal.tenant_id, document_id)
    if not retired:
        raise HTTPException(status_code=404, detail="没有已发布的知识文档可停用")
    await _audit(runtime, tenant_id=principal.tenant_id, user_id=principal.user_id, action="knowledge.document.retire", resource_id=document_id)
    return {"document_id": document_id, "status": "retired"}
