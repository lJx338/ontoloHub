"""SSO / Identity Provider models — HIA-79 D2.

Models an enterprise ``IdentityProvider`` (OIDC / SAML / LDAP) attached
to a ``Workspace``.  Each provider row carries its own protocol-specific
``config`` JSON (e.g. ``issuer_url`` + ``client_secret`` for OIDC) and
``claim_mapping`` (which IdP claims populate user fields).

A separate ``SsoLoginSession`` row tracks in-flight OIDC login flows
(state / nonce / redirect_uri).  The state is a CSRF nonce; the nonce
is the OIDC ID-token nonce.  Both are single-use and short-lived.

MVP scopes (D2):
  * Workspace-level provider config (one provider per workspace per protocol
    is enforced via a uniqueness constraint).
  * OIDC authorization-code flow with PKCE is the reference implementation.
  * ``force_sso`` (workspace-level) disables local-password login for any
    user that has a verified SSO identity in that workspace.
  * Just-In-Time provisioning: a new ``User`` is created on first login
    if ``auto_provision`` is true.  The user joins the workspace as
    ``MEMBER`` unless the provider claims a role override.
  * SAML / LDAP are stubbed at the data-model layer — their config
    schema is defined, but the auth flow ships in D2.x.

See ``docs/DEVELOPMENT.md`` §25 for the OIDC flow pitfalls and
field encryption guidance.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin

if TYPE_CHECKING:
    from .workspace import Workspace


# =====================================================================
# Enums
# =====================================================================


class SsoProtocol(str, Enum):
    """Which SSO protocol this provider speaks."""

    OIDC = "oidc"           # OpenID Connect (OAuth 2.0 + ID token)
    SAML = "saml"           # SAML 2.0 (stubbed in D2; full impl in D2.x)
    LDAP = "ldap"           # LDAP / Active Directory (stubbed in D2)


class SsoProviderStatus(str, Enum):
    """Health / lifecycle status of a configured provider."""

    ACTIVE = "active"       # in use; login flow is allowed
    DISABLED = "disabled"   # manually turned off
    ERROR = "error"         # last test_connection failed; needs admin attention


# =====================================================================
# IdentityProvider
# =====================================================================


class IdentityProvider(Base, UUIDMixin, TimestampMixin):
    """A configured Identity Provider (IdP) for a workspace.

    One workspace may have multiple providers (e.g. OIDC + SAML), but
    only one provider per protocol is enforced — admin can't create
    two OIDC providers for the same workspace without disabling the
    first.
    """

    __tablename__ = "identity_providers"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Display name; unique per workspace
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    protocol: Mapped[SsoProtocol] = mapped_column(
        String(20), nullable=False
    )
    status: Mapped[SsoProviderStatus] = mapped_column(
        String(20),
        default=SsoProviderStatus.ACTIVE.value,
        nullable=False,
    )

    # Protocol-specific config — see §25 in DEVELOPMENT.md for schema.
    # OIDC example: {
    #   "issuer_url": "https://example.okta.com",
    #   "client_id": "...",
    #   "client_secret_enc": "enc:v1:...",
    #   "scopes": ["openid", "profile", "email"],
    #   "authorization_endpoint": "...",
    #   "token_endpoint": "...",
    #   "userinfo_endpoint": "...",
    #   "jwks_uri": "..."
    # }
    # SAML / LDAP: stubbed; full schema lands in D2.x.
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Claim mapping: which IdP claim populates which User field.
    # Defaults are reasonable for OIDC: email=email, name=name.
    # Example: {"email": "email", "display_name": "name", "role": "groups"}
    claim_mapping: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )

    # Field names inside ``config`` that contain secrets and need
    # Fernet encryption at rest.  Used by the API to know what to
    # mask on read and encrypt on write.
    secret_fields: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )

    # Just-In-Time provisioning: if true, a new User is auto-created
    # on first successful SSO login.
    auto_provision: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # Force SSO: if true, this workspace refuses local-password login
    # for users that have a verified SSO identity in this workspace.
    # Bootstrap / global admins are exempt so they can recover a tenant.
    force_sso: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Last test_connection result + when it was run.  Useful for ops
    # dashboards.
    last_test_status: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )
    last_test_message: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    last_test_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    login_sessions: Mapped[list["SsoLoginSession"]] = relationship(
        "SsoLoginSession",
        back_populates="provider",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "protocol",
            name="uq_identity_providers_workspace_protocol",
        ),
        Index("ix_identity_providers_workspace", "workspace_id"),
        Index("ix_identity_providers_protocol", "protocol"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<IdentityProvider {self.name} {self.protocol.value} "
            f"workspace={self.workspace_id}>"
        )


# =====================================================================
# SsoLoginSession
# =====================================================================


class SsoLoginSession(Base, UUIDMixin, TimestampMixin):
    """An in-flight OIDC login flow.

    Created when the user clicks "Login with Okta" and we redirect
    them to the IdP.  Deleted / marked-used once the callback is
    processed.  Short TTL (default 10 minutes).

    For OIDC, ``state`` is the CSRF nonce echoed by the IdP; ``nonce``
    is embedded in the ID token to prevent replay.  ``code_verifier``
    is the PKCE verifier (its hash is sent as ``code_challenge`` on
    the authorize request).
    """

    __tablename__ = "sso_login_sessions"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity_providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # OIDC state (echoed by IdP) — also doubles as CSRF nonce
    state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # ID-token nonce
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    # PKCE verifier (RFC 7636) — its SHA-256 hash is the code_challenge
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    # Where to redirect after login (frontend URL)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Optional ``state`` carried from the front-end (e.g. workspace slug
    # the user picked on the login page)
    relay_state: Mapped[Optional[str]] = mapped_column(
        String(2048), nullable=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    provider: Mapped["IdentityProvider"] = relationship(
        "IdentityProvider", back_populates="login_sessions"
    )

    __table_args__ = (
        Index("ix_sso_login_sessions_state", "state"),
        Index("ix_sso_login_sessions_expires", "expires_at"),
    )

    @property
    def is_expired(self) -> bool:
        from datetime import datetime, timezone
        # SQLite drops tz info on read; treat naive as UTC.
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp < datetime.now(timezone.utc)

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SsoLoginSession state={self.state[:8]}... "
            f"provider={self.provider_id} expired={self.is_expired}>"
        )


__all__ = [
    "IdentityProvider",
    "SsoLoginSession",
    "SsoProtocol",
    "SsoProviderStatus",
]
