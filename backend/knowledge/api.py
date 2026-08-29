"""知识库文档管理 API（FastAPI 路由层）。

职责：
    - 提供文档的 列表 / 创建 / 发布 / 停用 四个 REST 端点（/knowledge 前缀）
    - 统一做权限校验（knowledge:read / knowledge:write）、限流与操作审计

关键设计：
    - 路由层只做"鉴权 -> 调仓库 -> 审计"的薄封装，业务逻辑全部下沉到
      KnowledgeRepository，保持 API 层无状态、可测试
    - 发布 / 停用这类状态变更由仓库层的版本号 + 状态机保证一致性
      （冲突对外表现为 409，而非 500）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.security import Principal, rate_limit_dependency

from .models import KnowledgeChunkInput, KnowledgeDocumentInput
from .repository import KnowledgeRepository

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class CreateKnowledgeDocumentRequest(BaseModel):
    """创建知识文档的请求体。

    字段：
        document: 文档元信息（见 models.KnowledgeDocumentInput）
        chunks: 1..500 个分块，超限直接 422 拒绝，
                防止单次请求体过大压垮嵌入与入库链路
    """

    model_config = ConfigDict(extra="forbid")

    document: KnowledgeDocumentInput
    chunks: list[KnowledgeChunkInput] = Field(min_length=1, max_length=500)


class PublishKnowledgeDocumentRequest(BaseModel):
    """发布请求体：指定要发布为正式版的文档版本号（>=1）。

    仓库层会校验该版本当前必须是 draft（状态机约束见 repository）。
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


def _runtime(request: Request):
    """从应用状态取运行时代理；知识库未装配时返回 503。

    request.app.state.runtime 在 lifespan 启动阶段注入（见 app.py / runtime.py），
    这里用 getattr 防御未初始化的情况，避免 500。
    """
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not hasattr(runtime, "knowledge"):
        raise HTTPException(status_code=503, detail="知识库服务尚未初始化")
    return runtime


def _require_scope(principal: Principal, scope: str) -> None:
    """校验主体是否拥有指定权限范围，缺失时抛 403。

    参数：
        principal: 已认证的调用主体（见 backend/security.py）
        scope: 所需权限名，如 "knowledge:read" / "knowledge:write"
    """
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"缺少 {scope} 权限")


async def _audit(runtime, *, tenant_id: str, user_id: str, action: str, resource_id: str) -> None:
    """记录管理操作审计事件；未装配审计服务时静默跳过（不阻塞主流程）。

    参数：tenant_id / user_id: 操作主体；action: 动作名；resource_id: 目标资源 ID
    """
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
    """分页列出当前租户的知识文档（按更新时间倒序）。

    参数：
        principal: 依赖注入的认证主体，同时承担限流职责
        limit: 每页条数（1..200）；offset: 跳过条数
    返回：{"items": [...]}，items 为文档摘要字典列表（不含分块内容）
    """
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
    """创建（或幂等覆盖）一份知识文档及其分块，成功后写审计。

    参数：payload: 文档元信息 + 1..500 个分块
    返回：201 + {"document_id", "version"}
    入库细节（UPSERT、分块重建）见 repository.put_document
    """
    _require_scope(principal, "knowledge:write")
    runtime = _runtime(request)
    repository: KnowledgeRepository = runtime.knowledge
    await repository.put_document(
        principal.tenant_id,
        payload.document,
        payload.chunks,
    )
    await _audit(
        runtime,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        action="knowledge.document.create",
        resource_id=payload.document.document_id,
    )
    return {"document_id": payload.document.document_id, "version": payload.document.version}


@router.post("/documents/{document_id}/publish")
async def publish_knowledge_document(
    document_id: str,
    payload: PublishKnowledgeDocumentRequest,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """将指定版本发布为正式版（同文档其他 published 版本会自动停用）。

    参数：
        document_id: 目标文档 ID
        payload.version: 要发布的版本号
    返回：{"document_id", "version", "status": "published"}
    异常：仓库层 ValueError（如版本非 draft）映射为 409 冲突
    """
    _require_scope(principal, "knowledge:write")
    runtime = _runtime(request)
    repository: KnowledgeRepository = runtime.knowledge
    try:
        await repository.publish_document_version(principal.tenant_id, document_id, payload.version)
    except ValueError as exc:
        # 版本状态机冲突（非 draft 不可发布）对外表现为 409 而非 500
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit(
        runtime,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        action="knowledge.document.publish",
        resource_id=document_id,
    )
    return {"document_id": document_id, "version": payload.version, "status": "published"}


@router.post("/documents/{document_id}/retire")
async def retire_knowledge_document(
    document_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """停用当前已发布版本（状态置为 retired，检索将不再命中）。

    参数：document_id: 目标文档 ID
    返回：{"document_id", "status": "retired"}
    异常：文档没有已发布版本时返回 404
    """
    _require_scope(principal, "knowledge:write")
    runtime = _runtime(request)
    repository: KnowledgeRepository = runtime.knowledge
    retired = await repository.retire_document(principal.tenant_id, document_id)
    if not retired:
        # 没有任何 published 版本可停用时，说明要么不存在、要么已是 retired
        raise HTTPException(status_code=404, detail="没有已发布的知识文档可停用")
    await _audit(
        runtime,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        action="knowledge.document.retire",
        resource_id=document_id,
    )
    return {"document_id": document_id, "status": "retired"}
