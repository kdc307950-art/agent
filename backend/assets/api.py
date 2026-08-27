"""IT asset management API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.security import Principal, rate_limit_dependency

from .models import AssetRecord, AssetStatus, CreateAsset, UpdateAsset
from .repository import AssetAlreadyExists, AssetNotFound


router = APIRouter(prefix="/assets", tags=["assets"])


def _runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not hasattr(runtime, "assets"):
        raise HTTPException(status_code=503, detail="资产服务尚未初始化")
    return runtime


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"缺少 {scope} 权限")


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AssetAlreadyExists):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="资产服务内部错误")


@router.get("")
async def list_assets(
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
    owner_user_id: str | None = Query(default=None, max_length=128),
    department: str | None = Query(default=None, max_length=128),
    asset_type: str | None = Query(default=None, max_length=64),
    status: AssetStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    _require_scope(principal, "asset:read")
    runtime = _runtime(request)
    # 客户（无 ticket:agent）只能查看本人名下资产，避免跨用户资产泄露。
    is_agent = "ticket:agent" in principal.scopes
    resolved_owner = owner_user_id if is_agent else principal.user_id
    items = await runtime.assets.list_assets(
        principal.tenant_id,
        owner_user_id=resolved_owner,
        department=department,
        asset_type=asset_type,
        status=status,
        limit=limit,
    )
    return {"items": items}


@router.post("", status_code=201)
async def create_asset(
    payload: CreateAsset,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "asset:write")
    runtime = _runtime(request)
    try:
        return await runtime.assets.create(principal.tenant_id, payload)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.patch("/{asset_id}")
async def update_asset(
    asset_id: str,
    payload: UpdateAsset,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "asset:write")
    runtime = _runtime(request)
    try:
        return await runtime.assets.update(principal.tenant_id, asset_id, payload)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "asset:write")
    runtime = _runtime(request)
    deleted = await runtime.assets.soft_delete(principal.tenant_id, asset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="资产不存在")
    return {"asset_id": asset_id, "deleted": True}
