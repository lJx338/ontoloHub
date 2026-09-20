"""SSO API — HIA-79 D2.

Two routers:

  * ``provider_router``  ``/api/workspaces/{wid}/sso/providers`` —
    workspace admin CRUD for ``IdentityProvider`` rows.  Secret
    fields (``client_secret`` for OIDC) are encrypted at rest and
    masked on read.

  * ``login_router``     ``/api/sso`` — public login flow.

    Login endpoints (no auth required):

      ``GET /api/sso/{workspace_slug}/login?provider=okta&return_to=...``
        → 302 to IdP authorize URL; PKCE challenge embedded.
      ``GET /api/sso/callback?code=...&state=...``
        → 302 to ``return_to`` with ``?token=...&refresh=...``;
        on error ``?error=<oidc_error_code>``.

Force-SSO is enforced inside ``login_router``'s callback: if the
target user is being provisioned into a workspace with
``force_sso=true`` and they have a verified SSO identity, the
``User.password_hash`` is *cleared* so local-password login is
disabled for them.

All other workspace API endpoints (members / projects / etc.) must
keep working unchanged.  This router does not modify those.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentPrincipal, get_current_user, record_audit
from src.api.workspace import require_workspace_role
from src.core.auth import (
    create_access_token,
    create_refresh_token,
    get_user_by_id,
)
from src.core.config import get_settings
from src.core.secrets import (
    decrypt_value,
    encrypt_value,
    mask_secret_fields,
)
from src.db.connection import get_session
from src.db.governance import AuditEventType
from src.db.identity import GlobalRole, User
from src.db.sso import (
    IdentityProvider,
    SsoLoginSession,
    SsoProtocol,
    SsoProviderStatus,
)
from src.db.workspace import Workspace, WorkspaceMembership, WorkspaceRole
from src.services import sso_client as sso_client_mod
from src.services.sso_client import (
    OidcError,
    build_authorization_url,
    generate_pkce_pair,
)

logger = logging.getLogger(__name__)

# =====================================================================
# Constants
# =====================================================================

# OIDC default scopes — minimum needed for our claim extraction.
DEFAULT_OIDC_SCOPES = ["openid", "profile", "email"]

# Default claim mapping (overrideable per provider).
DEFAULT_OIDC_CLAIM_MAPPING = {
    "email": "email",
    "display_name": "name",
    # If you want role-mapping, override claim_mapping.role = "groups"
}

# Login session lifetime.
SSO_LOGIN_TTL = timedelta(minutes=10)

# Default OIDC secret fields encrypted at rest.
# The OIDC spec calls this field ``client_secret``; storing under that
# same name keeps the contract symmetric (encrypt what the IdP gave us;
# mask the same field on read).
DEFAULT_OIDC_SECRET_FIELDS = ["client_secret"]


# =====================================================================
# Provider router (workspace-scoped)
# =====================================================================

provider_router = APIRouter(
    prefix="/api/workspaces", tags=["SSO"]
)


# ---------- request / response models ----------


class IdentityProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    protocol: str = Field(..., pattern="^(oidc|saml|ldap)$")
    config: dict = Field(default_factory=dict)
    claim_mapping: Optional[dict] = None
    secret_fields: Optional[list[str]] = None
    auto_provision: bool = True
    force_sso: bool = False


class IdentityProviderUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    config: Optional[dict] = None
    claim_mapping: Optional[dict] = None
    secret_fields: Optional[list[str]] = None
    auto_provision: Optional[bool] = None
    force_sso: Optional[bool] = None
    status: Optional[str] = Field(None, pattern="^(active|disabled)$")


class IdentityProviderResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    protocol: str
    status: str
    config: dict
    claim_mapping: dict
    secret_fields: list[str]
    auto_provision: bool
    force_sso: bool
    last_test_status: Optional[str]
    last_test_message: Optional[str]
    last_test_at: Optional[str]
    created_at: str
    updated_at: str


class TestConnectionResponse(BaseModel):
    ok: bool
    message: str


# ---------- helpers ----------


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


def _idp_to_response(idp: IdentityProvider) -> IdentityProviderResponse:
    """Mask secret fields before returning."""
    config = dict(idp.config or {})
    config = mask_secret_fields(config, idp.secret_fields or [])
    return IdentityProviderResponse(
        id=idp.id,
        workspace_id=idp.workspace_id,
        name=idp.name,
        protocol=idp.protocol,
        status=idp.status,
        config=config,
        claim_mapping=idp.claim_mapping or {},
        secret_fields=idp.secret_fields or [],
        auto_provision=bool(idp.auto_provision),
        force_sso=bool(idp.force_sso),
        last_test_status=idp.last_test_status,
        last_test_message=idp.last_test_message,
        last_test_at=_iso(idp.last_test_at) or None,
        created_at=_iso(idp.created_at),
        updated_at=_iso(idp.updated_at),
    )


def _resolve_actor_id(user: Any) -> Optional[uuid.UUID]:
    if hasattr(user, "user"):
        return user.user.id
    if hasattr(user, "id"):
        return user.id
    return None


def _encrypt_config_secrets(
    config: dict, secret_fields: list[str]
) -> dict:
    """Encrypt each secret field in-place.  Returns a new dict."""
    out = dict(config or {})
    for f in secret_fields or []:
        if f in out and isinstance(out[f], str) and not out[f].startswith("enc:"):
            out[f] = encrypt_value(out[f])
    return out


async def _load_workspace_by_id(
    session: AsyncSession, workspace_id: uuid.UUID
) -> Workspace:
    w = (
        await session.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
    ).scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="workspace not found")
    return w


async def _load_provider(
    session: AsyncSession, provider_id: uuid.UUID, workspace_id: uuid.UUID
) -> IdentityProvider:
    idp = (
        await session.execute(
            select(IdentityProvider).where(
                IdentityProvider.id == provider_id,
                IdentityProvider.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if not idp:
        raise HTTPException(status_code=404, detail="provider not found")
    return idp


# ---------- provider CRUD ----------


@provider_router.post(
    "/{workspace_id}/sso/providers",
    response_model=IdentityProviderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_provider(
    workspace_id: uuid.UUID,
    body: IdentityProviderCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: CurrentPrincipal = Depends(get_current_user),
) -> IdentityProviderResponse:
    """Workspace admin creates an IdP config.  ADMIN+ only."""
    await require_workspace_role(
        session, workspace_id, principal, WorkspaceRole.ADMIN
    )
    await _load_workspace_by_id(session, workspace_id)

    # Workspace already has a provider with this protocol?
    existing = (
        await session.execute(
            select(IdentityProvider).where(
                IdentityProvider.workspace_id == workspace_id,
                IdentityProvider.protocol == body.protocol,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace already has a {body.protocol} provider "
                f"(id={existing.id}); disable it first"
            ),
        )

    secret_fields = body.secret_fields or (
        DEFAULT_OIDC_SECRET_FIELDS if body.protocol == "oidc" else []
    )
    encrypted_config = _encrypt_config_secrets(body.config, secret_fields)

    actor_id = _resolve_actor_id(principal)

    idp = IdentityProvider(
        workspace_id=workspace_id,
        name=body.name,
        protocol=body.protocol,
        status=SsoProviderStatus.ACTIVE.value,
        config=encrypted_config,
        claim_mapping=body.claim_mapping or (
            DEFAULT_OIDC_CLAIM_MAPPING if body.protocol == "oidc" else {}
        ),
        secret_fields=secret_fields,
        auto_provision=body.auto_provision,
        force_sso=body.force_sso,
        created_by=actor_id,
    )
    session.add(idp)
    await session.flush()
    await session.refresh(idp)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        target_type="identity_provider",
        target_id=str(idp.id),
        target_label=idp.name,
        request=request,
        after={
            "protocol": idp.protocol,
            "force_sso": idp.force_sso,
            "auto_provision": idp.auto_provision,
        },
    )
    return _idp_to_response(idp)


@provider_router.get(
    "/{workspace_id}/sso/providers",
    response_model=list[IdentityProviderResponse],
)
async def list_providers(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: CurrentPrincipal = Depends(get_current_user),
) -> list[IdentityProviderResponse]:
    """List all IdPs in this workspace (any member can view)."""
    await require_workspace_role(
        session, workspace_id, principal, WorkspaceRole.VIEWER
    )
    rows = (
        await session.execute(
            select(IdentityProvider)
            .where(IdentityProvider.workspace_id == workspace_id)
            .order_by(IdentityProvider.created_at.asc())
        )
    ).scalars().all()
    return [_idp_to_response(r) for r in rows]


@provider_router.get(
    "/{workspace_id}/sso/providers/{provider_id}",
    response_model=IdentityProviderResponse,
)
async def get_provider(
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: CurrentPrincipal = Depends(get_current_user),
) -> IdentityProviderResponse:
    await require_workspace_role(
        session, workspace_id, principal, WorkspaceRole.VIEWER
    )
    idp = await _load_provider(session, provider_id, workspace_id)
    return _idp_to_response(idp)


@provider_router.patch(
    "/{workspace_id}/sso/providers/{provider_id}",
    response_model=IdentityProviderResponse,
)
async def update_provider(
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    body: IdentityProviderUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: CurrentPrincipal = Depends(get_current_user),
) -> IdentityProviderResponse:
    """Update a provider.  ADMIN+ only.  Secrets are re-encrypted."""
    await require_workspace_role(
        session, workspace_id, principal, WorkspaceRole.ADMIN
    )
    idp = await _load_provider(session, provider_id, workspace_id)

    if body.name is not None:
        idp.name = body.name
    if body.config is not None:
        merged_secret_fields = (
            body.secret_fields if body.secret_fields is not None
            else idp.secret_fields or []
        )
        merged = dict(idp.config or {})
        merged.update(body.config)
        idp.config = _encrypt_config_secrets(merged, merged_secret_fields)
        idp.secret_fields = merged_secret_fields
    if body.claim_mapping is not None:
        idp.claim_mapping = body.claim_mapping
    if body.secret_fields is not None:
        idp.secret_fields = body.secret_fields
    if body.auto_provision is not None:
        idp.auto_provision = body.auto_provision
    if body.force_sso is not None:
        idp.force_sso = body.force_sso
    if body.status is not None:
        idp.status = body.status

    await session.flush()
    await session.refresh(idp)

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        target_type="identity_provider",
        target_id=str(idp.id),
        target_label=idp.name,
        request=request,
        after={"force_sso": idp.force_sso, "status": idp.status},
    )
    return _idp_to_response(idp)


@provider_router.delete(
    "/{workspace_id}/sso/providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_provider(
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: CurrentPrincipal = Depends(get_current_user),
) -> None:
    """Delete an IdP.  ADMIN+ only.

    Any in-flight login sessions are cascade-deleted.
    """
    await require_workspace_role(
        session, workspace_id, principal, WorkspaceRole.ADMIN
    )
    idp = await _load_provider(session, provider_id, workspace_id)

    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=principal,
        target_type="identity_provider",
        target_id=str(idp.id),
        target_label=idp.name,
        request=request,
    )
    await session.delete(idp)
    await session.flush()


@provider_router.post(
    "/{workspace_id}/sso/providers/{provider_id}/test",
    response_model=TestConnectionResponse,
)
async def test_provider(
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: CurrentPrincipal = Depends(get_current_user),
) -> TestConnectionResponse:
    """Test connection to the IdP.  OIDC: fetch discovery + verify
    signing JWKS reachable.  SAML / LDAP: stubbed (returns ``ok=False``).
    """
    await require_workspace_role(
        session, workspace_id, principal, WorkspaceRole.ADMIN
    )
    idp = await _load_provider(session, provider_id, workspace_id)

    message = ""
    ok = False
    if idp.protocol == SsoProtocol.OIDC.value:
        try:
            issuer_url = (idp.config or {}).get("issuer_url", "")
            if not issuer_url:
                raise OidcError("missing_field", "config.issuer_url is empty")
            discovery = await sso_client_mod.fetch_discovery(issuer_url, force=True)
            ok = True
            message = (
                f"OIDC discovery OK; issuer={discovery.issuer}; "
                f"jwks={discovery.jwks_uri}"
            )
        except OidcError as exc:
            ok = False
            message = f"{exc.code}: {exc.message}"
    else:
        ok = False
        message = f"{idp.protocol} test_connection not implemented in D2"

    idp.last_test_status = "ok" if ok else "error"
    idp.last_test_message = message
    idp.last_test_at = datetime.now(timezone.utc)
    await session.flush()

    return TestConnectionResponse(ok=ok, message=message)


# =====================================================================
# Login router (public)
# =====================================================================

login_router = APIRouter(prefix="/api/sso", tags=["SSO"])


async def _load_workspace_by_slug(
    session: AsyncSession, slug: str
) -> Workspace:
    w = (
        await session.execute(
            select(Workspace).where(Workspace.slug == slug)
        )
    ).scalar_one_or_none()
    if not w or w.deleted_at is not None:
        # Don't leak existence — 404 either way
        raise HTTPException(status_code=404, detail="workspace not found")
    return w


def _validate_return_to(return_to: Optional[str]) -> str:
    """Reject absolute / off-site return_to URLs (open-redirect guard).

    Default to "/" when missing.
    """
    settings = get_settings()
    if not return_to:
        return "/"
    parsed = urlparse(return_to)
    if parsed.scheme or parsed.netloc:
        # Caller passed an absolute URL — drop to safe default.
        return "/"
    if not return_to.startswith("/"):
        return_to = "/" + return_to.lstrip("/")
    # Frontend root lives at settings.api.frontend_root (configurable).
    return return_to


@login_router.get("/{workspace_slug}/login")
async def sso_login(
    workspace_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    provider_id: Optional[uuid.UUID] = Query(
        None, description="Specific provider id; if absent the first ACTIVE one is used"
    ),
    return_to: Optional[str] = Query(None),
) -> RedirectResponse:
    """Begin SSO login for the workspace.  Redirects to IdP.

    Steps:
      1. Resolve workspace by slug
      2. Resolve the (active, OIDC) provider
      3. Generate PKCE pair, state, nonce
      4. Persist ``SsoLoginSession`` row (so we can verify state at callback)
      5. 302 to IdP authorize URL
    """
    w = await _load_workspace_by_slug(session, workspace_slug)

    if provider_id is not None:
        idp = await _load_provider(session, provider_id, w.id)
    else:
        idp = (
            await session.execute(
                select(IdentityProvider).where(
                    IdentityProvider.workspace_id == w.id,
                    IdentityProvider.protocol == SsoProtocol.OIDC.value,
                    IdentityProvider.status == SsoProviderStatus.ACTIVE.value,
                )
            )
        ).scalar_one_or_none()

    if idp is None:
        raise HTTPException(
            status_code=404, detail="no active OIDC provider for workspace"
        )
    if idp.protocol != SsoProtocol.OIDC.value:
        raise HTTPException(
            status_code=400,
            detail=f"login flow for {idp.protocol} not implemented in D2",
        )

    issuer = (idp.config or {}).get("issuer_url", "")
    if not issuer:
        raise HTTPException(status_code=409, detail="provider config missing issuer_url")
    client_id = (idp.config or {}).get("client_id", "")
    if not client_id:
        raise HTTPException(status_code=409, detail="provider config missing client_id")

    # Pull endpoints from discovery.  This is the only outbound call we
    # make at login-start time; downstream uses cached values.
    discovery = await sso_client_mod.fetch_discovery(issuer, force=False)

    settings = get_settings()
    # Default redirect_uri = backend /api/sso/callback.
    # Admins can override per-provider via config.redirect_uri.
    redirect_uri = (idp.config or {}).get("redirect_uri") or (
        f"{request.url.scheme}://{request.url.netloc}/api/sso/callback"
    )
    code_verifier, code_challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    # Sanitise return_to at login time, too — we don't want a malicious
    # return_to URL to survive in the DB even if a future code path
    # forgets to re-validate at callback time.  Defensive in depth.
    safe_relay_state = _validate_return_to(return_to)

    sso_session = SsoLoginSession(
        provider_id=idp.id,
        workspace_id=w.id,
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        relay_state=safe_relay_state,
        expires_at=datetime.now(timezone.utc) + SSO_LOGIN_TTL,
    )
    session.add(sso_session)
    await session.flush()

    scopes = (idp.config or {}).get("scopes") or DEFAULT_OIDC_SCOPES
    auth_url = build_authorization_url(
        authorization_endpoint=discovery.authorization_endpoint,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=code_challenge,
        scopes=scopes,
    )
    return RedirectResponse(url=auth_url, status_code=302)


@login_router.get("/callback")
async def sso_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """IdP redirects here with ``?code=...&state=...``.

    We:
      1. Look up ``SsoLoginSession`` by state (CSRF check)
      2. Verify not expired / consumed
      3. Exchange code for tokens
      4. Verify ID token (signature + claims + nonce)
      5. Find or provision a ``User``
      6. Add them as ``WorkspaceMembership`` if missing
      7. Issue JWT access + refresh
      8. 302 to ``return_to?token=...&refresh=...``
    """
    if error:
        # IdP-side error (user denied, etc.)
        return _redirect_with_error(return_to=None, code=error or "idp_error",
                                     message=error_description or "")

    if not code or not state:
        raise HTTPException(
            status_code=400, detail="missing code or state"
        )

    sso_session = (
        await session.execute(
            select(SsoLoginSession).where(SsoLoginSession.state == state)
        )
    ).scalar_one_or_none()
    if sso_session is None:
        # Could be replay / random guess — 400 with no detail
        raise HTTPException(status_code=400, detail="invalid state")
    if sso_session.is_expired:
        raise HTTPException(status_code=400, detail="login session expired")
    if sso_session.is_consumed:
        raise HTTPException(
            status_code=400, detail="login session already used"
        )

    idp = (
        await session.execute(
            select(IdentityProvider).where(
                IdentityProvider.id == sso_session.provider_id
            )
        )
    ).scalar_one_or_none()
    if idp is None or idp.status != SsoProviderStatus.ACTIVE.value:
        raise HTTPException(status_code=409, detail="provider not active")

    # Decrypt the client secret.
    plain_cfg = dict(idp.config or {})
    if idp.secret_fields:
        for f in idp.secret_fields:
            if f in plain_cfg and isinstance(plain_cfg[f], str):
                plain_cfg[f] = decrypt_value(plain_cfg[f])
    # Provider may store the secret under either ``client_secret`` or
    # ``client_secret_enc`` — alias for clarity.
    if "client_secret" not in plain_cfg and "client_secret_enc" in plain_cfg:
        plain_cfg["client_secret"] = plain_cfg["client_secret_enc"]

    issuer = plain_cfg.get("issuer_url", "")
    client_id = plain_cfg.get("client_id", "")
    client_secret = plain_cfg.get("client_secret", "")

    try:
        discovery = await sso_client_mod.fetch_discovery(issuer, force=False)
        tokens = await sso_client_mod.exchange_code_for_tokens(
            token_endpoint=discovery.token_endpoint,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=sso_session.redirect_uri,
            code_verifier=sso_session.code_verifier,
        )
        claims = await sso_client_mod.verify_id_token(
            id_token=tokens.id_token,
            issuer=issuer,
            client_id=client_id,
            jwks_uri=discovery.jwks_uri,
            expected_nonce=sso_session.nonce,
            access_token=tokens.access_token,
        )
    except OidcError as exc:
        # Surface IdP / verification errors back to the frontend.
        return _redirect_with_error(
            return_to=sso_session.relay_state,
            code=exc.code, message=exc.message,
        )

    # Mark login session consumed so a replay can't happen.
    sso_session.consumed_at = datetime.now(timezone.utc)
    await session.flush()

    # Map IdP claims → User fields using claim_mapping.
    mapping = idp.claim_mapping or DEFAULT_OIDC_CLAIM_MAPPING
    user_info: dict[str, Any] = dict(claims.raw or {})
    if discovery.userinfo_endpoint:
        # Optional enrichment — best-effort.
        user_info.update(
            await sso_client_mod.fetch_userinfo(
                userinfo_endpoint=discovery.userinfo_endpoint,
                access_token=tokens.access_token,
            )
        )

    def _claim(field: str) -> Optional[str]:
        claim_name = mapping.get(field) or field
        value = user_info.get(claim_name)
        return str(value) if value is not None else None

    email = _claim("email")
    if not email:
        return _redirect_with_error(
            return_to=sso_session.relay_state,
            code="missing_email",
            message="IdP did not return an email claim",
        )
    email = email.strip().lower()

    display_name = (
        _claim("display_name")
        or _claim("name")
        or claims.preferred_username
        or email.split("@")[0]
    )

    # Find or provision user.
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    is_new_user = False
    if user is None:
        if not idp.auto_provision:
            await record_audit(
                session,
                event_type=AuditEventType.CREATE,
                principal=CurrentPrincipal(user=None, is_admin=False) if False else _sys_principal(),
                target_type="sso_login_rejected",
                target_id=claims.sub,
                target_label=email,
                request=request,
                after={
                    "reason": "auto_provision=false",
                    "workspace_id": str(sso_session.workspace_id),
                },
            )
            return _redirect_with_error(
                return_to=sso_session.relay_state,
                code="user_not_provisioned",
                message="workspace has auto_provision disabled",
            )
        user = User(
            email=email,
            display_name=display_name,
            global_role=GlobalRole.USER.value,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        is_new_user = True

    if not user.is_active:
        return _redirect_with_error(
            return_to=sso_session.relay_state,
            code="user_disabled",
            message="user account is disabled",
        )

    # Workspace membership.
    existing_membership = (
        await session.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == sso_session.workspace_id,
                WorkspaceMembership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing_membership is None:
        m = WorkspaceMembership(
            user_id=user.id,
            workspace_id=sso_session.workspace_id,
            role=WorkspaceRole.MEMBER.value,
            is_active=True,
        )
        session.add(m)
        await session.flush()

    # Force-SSO enforcement: if the workspace's IdP says force_sso, clear
    # any local-password hash on this user so they can't bypass SSO.
    workspace = (
        await session.execute(
            select(Workspace).where(Workspace.id == sso_session.workspace_id)
        )
    ).scalar_one()
    if idp.force_sso and user.password_hash:
        user.password_hash = None
        await session.flush()

    # Issue tokens.
    user.last_login_at = datetime.now(timezone.utc)
    session.add(user)
    await session.flush()

    settings = get_settings()
    access = create_access_token(subject=str(user.id), extra={"email": user.email})
    refresh = create_refresh_token(subject=str(user.id))

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=CurrentPrincipal(user=user, is_admin=user.global_role == "admin"),
        target_type="sso_login",
        target_id=str(user.id),
        target_label=email,
        request=request,
        after={
            "provider_id": str(idp.id),
            "provider_name": idp.name,
            "workspace_id": str(sso_session.workspace_id),
            "sub": claims.sub,
            "is_new_user": is_new_user,
            "force_sso": idp.force_sso,
        },
    )

    # Redirect to return_to with tokens in the query string.
    target = _validate_return_to(sso_session.relay_state)
    sep = "&" if "?" in target else "?"
    target = f"{target}{sep}token={quote(access)}&refresh={quote(refresh)}"
    return RedirectResponse(url=target, status_code=302)


def _redirect_with_error(
    *, return_to: Optional[str], code: str, message: str
) -> RedirectResponse:
    target = _validate_return_to(return_to)
    sep = "&" if "?" in target else "?"
    # Encode the message into the query string for the frontend to display.
    safe_msg = quote(message or "", safe="")
    target = f"{target}{sep}sso_error={quote(code)}&sso_error_message={safe_msg}"
    return RedirectResponse(url=target, status_code=302)


def _sys_principal() -> CurrentPrincipal:
    """A best-effort principal for system-initiated audit events (e.g.
    user-not-provisioned rejections).  We don't have a real user, so
    we synthesise a CurrentPrincipal whose ``user.id`` is None."""
    # ``record_audit`` only reads ``user.id``; pass a stub.  The empty
    # User() row is fine — nothing will be persisted.
    from src.api.auth import CurrentPrincipal
    from src.db.identity import User as _User
    stub = _User(id=None)  # type: ignore[call-arg]
    return CurrentPrincipal(user=stub, is_admin=False)


__all__ = [
    "provider_router",
    "login_router",
    "IdentityProviderCreate",
    "IdentityProviderResponse",
]
