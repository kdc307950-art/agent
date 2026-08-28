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


async def _require_asset_access(runtime, principal: Principal, asset_id: str):
    """按当前主体校验资产可读性。

    客服（ticket:agent）可读取租户内任意资产；客户只能读取本人名下资产，
    否则一律返回 404（与工单读取逻辑一致，避免跨用户资产枚举）。
    """
    asset = await runtime.assets.get(principal.tenant_id, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if "ticket:agent" not in principal.scopes and asset.owner_user_id != principal.user_id:
        raise HTTPException(status_code=404, detail="资产不存在")
    return asset


async def _audit(runtime, *, tenant_id: str, user_id: str, action: str, resource_id: str, detail: dict | None = None) -> None:
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    await audit.record_admin_event(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource_type="asset",
        resource_id=resource_id,
        detail=detail,
    )


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


@router.get("/{asset_id}")
async def get_asset(
    asset_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "asset:read")
    runtime = _runtime(request)
    return await _require_asset_access(runtime, principal, asset_id)


@router.get("/{asset_id}/tickets")
async def list_asset_tickets(
    asset_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "asset:read")
    runtime = _runtime(request)
    await _require_asset_access(runtime, principal, asset_id)
    # 第二道隔离：客服查看全部关联工单；客户只查看自己的工单。
    # 即使客服误把某人的资产关联到另一人的工单，资产主人也看不到不属于自己的工单。
    is_agent = "ticket:agent" in principal.scopes
    tickets = await runtime.tickets.list_tickets(
        principal.tenant_id,
        asset_id=asset_id,
        requester_id=None if is_agent else principal.user_id,
        limit=50,
    )
    return {"items": tickets}


@router.post("", status_code=201)
async def create_asset(
    payload: CreateAsset,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    _require_scope(principal, "asset:write")
    runtime = _runtime(request)
    try:
        created = await runtime.assets.create(principal.tenant_id, payload)
    except Exception as exc:
        raise _map_error(exc) from exc
    await _audit(runtime, tenant_id=principal.tenant_id, user_id=principal.user_id, action="asset.create", resource_id=created.asset_id, detail={"asset_no": created.asset_no})
    return created


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
        updated = await runtime.assets.update(principal.tenant_id, asset_id, payload)
    except Exception as exc:
        raise _map_error(exc) from exc
    await _audit(runtime, tenant_id=principal.tenant_id, user_id=principal.user_id, action="asset.update", resource_id=asset_id)
    return updated


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
    await _audit(runtime, tenant_id=principal.tenant_id, user_id=principal.user_id, action="asset.delete", resource_id=asset_id)
    return {"asset_id": asset_id, "deleted": True}
