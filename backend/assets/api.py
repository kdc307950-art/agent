"""IT 资产（Asset）管理 HTTP API —— 资产台账的读写网关。

职责：
    - 提供 /assets 下的资产路由：列表 / 详情 / 关联工单 / 创建 / 更新 / 软删除
    - 按 scope 鉴权：读操作要求 asset:read，写操作要求 asset:write
    - 写操作统一写入管理审计（audit.record_admin_event）

关键设计：
    - 租户隔离：所有查询都以 principal.tenant_id 为第一过滤条件，绝不跨租户访问
    - 防枚举：客户（无 ticket:agent）访问他人资产一律返回 404，与工单读取口径一致
    - 异常归一：_map_error 把仓储层异常（NotFound / AlreadyExists）映射为 HTTP 状态码
    - 删除为软删除（soft_delete），数据不物理移除，便于审计追溯
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.security import Principal, rate_limit_dependency

from .models import AssetStatus, CreateAsset, UpdateAsset
from .repository import AssetAlreadyExists, AssetNotFound

router = APIRouter(prefix="/assets", tags=["assets"])


def _runtime(request: Request):
    """从 FastAPI 应用状态中取运行时容器。

    应用启动早期（lifespan 装配完成前）可能还没有 runtime；此时返回 503
    而不是 500，语义上表示「服务未就绪」。
    """
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not hasattr(runtime, "assets"):
        raise HTTPException(status_code=503, detail="资产服务尚未初始化")
    return runtime


def _require_scope(principal: Principal, scope: str) -> None:
    """校验当前主体是否拥有指定 scope，缺失则抛 403。

    属于路由层的第一道权限门；第二道门在数据层（_require_asset_access
    按 owner 收窄可见范围）。
    """
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"缺少 {scope} 权限")


def _map_error(exc: Exception) -> HTTPException:
    """把仓储层异常归一为 HTTP 错误：NotFound→404、AlreadyExists→409、其余→500。

    这样路由函数只需要 try/except Exception 后调用本函数，避免在各处
    重复编写状态码映射逻辑。
    """
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


async def _audit(
    runtime,
    *,
    tenant_id: str,
    user_id: str,
    action: str,
    resource_id: str,
    detail: dict | None = None,
) -> None:
    """写入一条资产相关管理审计记录（action 形如 asset.create）。

    审计组件是可选的：运行时未装配 audit 时静默跳过，不影响主流程。
    """
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
    """列出资产（GET /assets）。

    参数：owner_user_id / department / asset_type / status 为可选过滤条件，
    limit 控制返回条数（1~500，默认 100）。
    返回：{"items": [AssetRecord, ...]}。
    设计：客服可自由过滤；客户强制把 owner 收窄为本人，防止跨用户资产泄露。
    """
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
    """按 ID 获取单个资产（GET /assets/{asset_id}）。

    返回：AssetRecord，或 404（不存在 / 客户无权读取他人资产）。
    设计：先 _require_scope 校验权限，再 _require_asset_access 按主体
    收窄可见范围，两道闸门保证客户无法枚举他人资产。
    """
    _require_scope(principal, "asset:read")
    runtime = _runtime(request)
    return await _require_asset_access(runtime, principal, asset_id)


@router.get("/{asset_id}/tickets")
async def list_asset_tickets(
    asset_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """列出某资产关联的工单（GET /assets/{asset_id}/tickets）。

    参数：无；返回：{"items": [工单, ...]}，最多 50 条。
    设计：先校验资产可读，再做第二道隔离——客服看全部关联工单，
    客户只看本人发起的工单，防止资产与工单错配导致越权可见。
    """
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
    """创建资产（POST /assets，201）。

    参数：CreateAsset 请求体（asset_id 需唯一）。
    返回：新创建的 AssetRecord；资产编号或 ID 冲突返回 409。
    设计：写成功后记录 asset.create 审计，便于后续追溯资产录入来源。
    """
    _require_scope(principal, "asset:write")
    runtime = _runtime(request)
    try:
        # 仓储层异常（AlreadyExists 等）统一映射为 HTTP 状态码。
        created = await runtime.assets.create(principal.tenant_id, payload)
    except Exception as exc:
        raise _map_error(exc) from exc
    await _audit(
        runtime,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        action="asset.create",
        resource_id=created.asset_id,
        detail={"asset_no": created.asset_no},
    )
    return created


@router.patch("/{asset_id}")
async def update_asset(
    asset_id: str,
    payload: UpdateAsset,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """局部更新资产（PATCH /assets/{asset_id}）。

    参数：UpdateAsset 请求体——仅更新显式提供的字段，传 null 表示清空
    （见 repository.update 的 exclude_unset 设计）。
    返回：更新后的 AssetRecord；资产不存在返回 404，编号冲突返回 409。
    """
    _require_scope(principal, "asset:write")
    runtime = _runtime(request)
    try:
        updated = await runtime.assets.update(principal.tenant_id, asset_id, payload)
    except Exception as exc:
        raise _map_error(exc) from exc
    await _audit(
        runtime,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        action="asset.update",
        resource_id=asset_id,
    )
    return updated


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: str,
    request: Request,
    principal: Principal = Depends(rate_limit_dependency),
):
    """软删除资产（DELETE /assets/{asset_id}）。

    返回：{"asset_id": ..., "deleted": True}；资产不存在返回 404。
    设计：仅置 is_deleted 标记而非物理删除，保留历史记录供审计追溯。
    """
    _require_scope(principal, "asset:write")
    runtime = _runtime(request)
    deleted = await runtime.assets.soft_delete(principal.tenant_id, asset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="资产不存在")
    await _audit(
        runtime,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        action="asset.delete",
        resource_id=asset_id,
    )
    return {"asset_id": asset_id, "deleted": True}
