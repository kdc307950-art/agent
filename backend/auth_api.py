"""Authentication bootstrap endpoints for the Web workbench.

The production API remains a bearer-token resource server. This module only
exposes public configuration, a development-only persona selector, and an
authenticated identity endpoint so the React application never needs to infer
tenant or permission data from a token payload.
"""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .security import Principal, authenticate, make_tenant_token, scopes_for_dev_role

router = APIRouter(prefix="/auth", tags=["auth"])


class PrincipalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    scopes: list[str]
    departments: list[str]
    internal: bool


class OIDCWebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    redirect_uri: str
    authorization_endpoint: str
    token_endpoint: str
    scopes: list[str]


class AuthConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_mode: Literal["dev", "oidc"]
    demo_login_enabled: bool
    oidc: OIDCWebConfig | None = None


class DevSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona: Literal["customer", "agent", "admin"]


class DevSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    expires_in: int
    principal: PrincipalResponse


_DEMO_PERSONAS: dict[str, dict[str, object]] = {
    "customer": {
        "user_id": "customer-1",
        "role": "helpdesk-customer",
        "departments": ("general",),
        "internal": False,
    },
    "agent": {
        "user_id": "agent-1",
        "role": "helpdesk-agent",
        "departments": ("it",),
        "internal": True,
    },
    "admin": {
        "user_id": "admin-1",
        "role": "helpdesk-it-admin",
        "departments": ("it",),
        "internal": True,
    },
}


def _principal_response(principal: Principal) -> PrincipalResponse:
    return PrincipalResponse(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        scopes=sorted(principal.scopes),
        departments=sorted(principal.departments),
        internal=principal.internal,
    )


@router.get("/config", response_model=AuthConfigResponse)
async def get_auth_config(request: Request) -> AuthConfigResponse:
    """Return only public login configuration needed by the browser."""
    settings = request.app.state.settings
    oidc = None
    if settings.auth_mode == "oidc" and settings.oidc_web_configured:
        oidc = OIDCWebConfig(
            client_id=settings.oidc_client_id or "",
            redirect_uri=settings.oidc_redirect_uri or "",
            authorization_endpoint=settings.oidc_authorization_endpoint or "",
            token_endpoint=settings.oidc_token_endpoint or "",
            scopes=sorted(settings.oidc_web_scopes),
        )
    return AuthConfigResponse(
        auth_mode=settings.auth_mode,
        demo_login_enabled=settings.dev_demo_login_enabled,
        oidc=oidc,
    )


@router.post("/dev/session", response_model=DevSessionResponse)
async def create_dev_session(payload: DevSessionRequest, request: Request) -> DevSessionResponse:
    """Issue a fixed development persona token; never enabled outside local development."""
    settings = request.app.state.settings
    if not (
        settings.app_env == "development"
        and settings.auth_mode == "dev"
        and settings.dev_demo_login_enabled
    ):
        raise HTTPException(status_code=404, detail="开发演示登录未启用")

    persona = _DEMO_PERSONAS[payload.persona]
    role = str(persona["role"])
    user_id = str(persona["user_id"])
    departments = cast(tuple[str, ...], persona["departments"])
    internal = bool(persona["internal"])
    token = make_tenant_token(
        "demo",
        user_id,
        settings.tenant_token_secret or "",
        scopes=scopes_for_dev_role(role),
        departments=departments,
        internal=internal,
        ttl_seconds=settings.dev_demo_token_ttl_seconds,
    )
    principal = Principal(
        tenant_id="demo",
        user_id=user_id,
        scopes=frozenset(scopes_for_dev_role(role)),
        departments=frozenset(departments),
        internal=internal,
    )
    return DevSessionResponse(
        access_token=token,
        expires_in=settings.dev_demo_token_ttl_seconds,
        principal=_principal_response(principal),
    )


@router.get("/me", response_model=PrincipalResponse)
async def get_current_principal(principal: Principal = Depends(authenticate)) -> PrincipalResponse:
    return _principal_response(principal)
