"""OIDC client helpers — HIA-79 D2.

Minimal OpenID Connect authorization-code + PKCE client.  We avoid
heavy dependencies (no ``authlib``) and build on ``httpx`` (already
required by the rest of the project) plus ``python-jose`` for JWT
verification.

What's implemented here:
  * OIDC discovery (``/.well-known/openid-configuration``) cache
  * PKCE code-verifier / code-challenge (RFC 7636, S256)
  * Authorization URL builder
  * Code → token exchange (authorization_code grant)
  * ID-token JWT signature verification via JWKS
  * ``claims`` extraction: ``sub`` / ``email`` / ``email_verified`` /
    ``name`` / ``preferred_username``

Out of scope (deferred to D2.x):
  * SAML
  * LDAP / AD bind
  * Refresh-token storage (revoked on user logout)

Pitfalls collected from this implementation live in
``docs/DEVELOPMENT.md`` §25 — please read before extending.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from jose import jwt
from jose.exceptions import JWTError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class OidcError(Exception):
    """A failure inside the OIDC client.  Detail is safe to return
    to the caller (no secret / no key material)."""

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------
# PKCE (RFC 7636)
# ---------------------------------------------------------------------


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a fresh (verifier, challenge) pair.

    The verifier is 32 random bytes (43-char base64url after padding
    stripped).  The challenge is the S256 hash — IdP default and
    only mode we accept.
    """
    verifier = _b64url_no_pad(secrets.token_bytes(32))
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ---------------------------------------------------------------------
# Discovery cache
# ---------------------------------------------------------------------


@dataclass
class OidcDiscovery:
    """Cached ``.well-known/openid-configuration`` document.

    ``endpoints`` is the raw JSON; helper properties expose the bits
    we need (authorize / token / jwks / userinfo).
    """

    endpoints: dict[str, Any]
    fetched_at: float

    @property
    def issuer(self) -> str:
        return self.endpoints.get("issuer", "")

    @property
    def authorization_endpoint(self) -> str:
        return self.endpoints["authorization_endpoint"]

    @property
    def token_endpoint(self) -> str:
        return self.endpoints["token_endpoint"]

    @property
    def jwks_uri(self) -> str:
        return self.endpoints["jwks_uri"]

    @property
    def userinfo_endpoint(self) -> Optional[str]:
        return self.endpoints.get("userinfo_endpoint")


_DISCOVERY_TTL_SECONDS = 3600
_DISCOVERY_CACHE: dict[str, OidcDiscovery] = {}


async def fetch_discovery(issuer_url: str, *, force: bool = False) -> OidcDiscovery:
    """Return (and cache) the IdP discovery document.

    Cache key is the issuer URL.  Cache is process-local; we do not
    push it to Redis because issuer endpoints change rarely and the
    refresh cost is small.
    """
    issuer = issuer_url.rstrip("/")
    cached = _DISCOVERY_CACHE.get(issuer)
    if cached is not None and not force and (
        time.monotonic() - cached.fetched_at < _DISCOVERY_TTL_SECONDS
    ):
        return cached

    url = f"{issuer}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise OidcError(
            "discovery_unreachable",
            f"failed to fetch {url}: {type(exc).__name__}",
            status_code=502,
        ) from exc

    if resp.status_code != 200:
        raise OidcError(
            "discovery_failed",
            f"discovery returned HTTP {resp.status_code}",
            status_code=502,
        )

    try:
        doc = resp.json()
    except json.JSONDecodeError as exc:
        raise OidcError(
            "discovery_invalid_json",
            f"discovery document is not valid JSON: {exc}",
            status_code=502,
        ) from exc

    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if required not in doc:
            raise OidcError(
                "discovery_missing_field",
                f"discovery doc missing required field '{required}'",
                status_code=502,
            )

    discovery = OidcDiscovery(endpoints=doc, fetched_at=time.monotonic())
    _DISCOVERY_CACHE[issuer] = discovery
    return discovery


def clear_discovery_cache() -> None:
    """Reset the discovery cache (used by tests)."""
    _DISCOVERY_CACHE.clear()


# ---------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------


_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_TTL_SECONDS = 600


async def fetch_jwks(jwks_uri: str, *, force: bool = False) -> dict[str, Any]:
    cached = _JWKS_CACHE.get(jwks_uri)
    if cached is not None and not force and (
        time.monotonic() - cached[0] < _JWKS_TTL_SECONDS
    ):
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(jwks_uri)
    except httpx.HTTPError as exc:
        raise OidcError(
            "jwks_unreachable",
            f"failed to fetch {jwks_uri}: {type(exc).__name__}",
            status_code=502,
        ) from exc

    if resp.status_code != 200:
        raise OidcError(
            "jwks_failed", f"JWKS returned HTTP {resp.status_code}", status_code=502
        )

    try:
        doc = resp.json()
    except json.JSONDecodeError as exc:
        raise OidcError(
            "jwks_invalid_json", f"JWKS is not valid JSON: {exc}", status_code=502
        ) from exc

    if "keys" not in doc or not isinstance(doc["keys"], list):
        raise OidcError(
            "jwks_invalid_shape", "JWKS doc missing 'keys' array", status_code=502
        )

    _JWKS_CACHE[jwks_uri] = (time.monotonic(), doc)
    return doc


def clear_jwks_cache() -> None:
    """Reset the JWKS cache (used by tests)."""
    _JWKS_CACHE.clear()


# ---------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    scopes: list[str],
) -> str:
    """Build the URL we redirect the browser to.

    Standard params: response_type=code, scope, client_id, redirect_uri,
    state, nonce, code_challenge, code_challenge_method=S256.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


# ---------------------------------------------------------------------
# Code → token
# ---------------------------------------------------------------------


@dataclass
class OidcTokenResponse:
    access_token: str
    id_token: str
    refresh_token: Optional[str]
    expires_in: Optional[int]
    token_type: str
    raw: dict[str, Any]


async def exchange_code_for_tokens(
    *,
    token_endpoint: str,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: str,
) -> OidcTokenResponse:
    """Exchange an authorization code for tokens.

    The verifier is sent as ``code_verifier``; the IdP checks the
    SHA-256 hash matches what we sent on the authorize request.
    """
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(
                token_endpoint,
                data=body,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise OidcError(
            "token_endpoint_unreachable",
            f"failed to reach {token_endpoint}: {type(exc).__name__}",
            status_code=502,
        ) from exc

    if resp.status_code != 200:
        # IdP typically returns OAuth2 error JSON; surface the code
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = {}
        err = payload.get("error", "unknown_error")
        raise OidcError(
            "token_exchange_failed",
            f"IdP returned '{err}' (HTTP {resp.status_code})",
            status_code=502,
        )

    try:
        doc = resp.json()
    except json.JSONDecodeError as exc:
        raise OidcError(
            "token_exchange_invalid_json",
            f"token response is not valid JSON: {exc}",
            status_code=502,
        ) from exc

    if "access_token" not in doc or "id_token" not in doc:
        raise OidcError(
            "token_exchange_missing_field",
            "token response missing access_token or id_token",
            status_code=502,
        )

    return OidcTokenResponse(
        access_token=doc["access_token"],
        id_token=doc["id_token"],
        refresh_token=doc.get("refresh_token"),
        expires_in=doc.get("expires_in"),
        token_type=doc.get("token_type", "Bearer"),
        raw=doc,
    )


# ---------------------------------------------------------------------
# ID-token verification
# ---------------------------------------------------------------------


@dataclass
class OidcClaims:
    sub: str
    issuer: str
    audience: Any
    nonce: Optional[str]
    email: Optional[str]
    email_verified: Optional[bool]
    name: Optional[str]
    preferred_username: Optional[str]
    raw: dict[str, Any]


async def verify_id_token(
    *,
    id_token: str,
    issuer: str,
    client_id: str,
    jwks_uri: str,
    expected_nonce: str,
    access_token: Optional[str] = None,
) -> OidcClaims:
    """Verify an ID token signature + standard claims.

    Uses JWKS for the signing key.  Validates ``iss``, ``aud``,
    ``exp``, ``iat``, ``nonce``.  Optionally verifies the
    ``at_hash`` claim if an ``access_token`` is supplied.

    Returns a parsed ``OidcClaims``; raises ``OidcError`` on any
    failure (caller should redirect to /login with an error).
    """
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise OidcError("id_token_invalid_header", f"can't parse header: {exc}") from exc

    kid = unverified_header.get("kid")
    if not kid:
        raise OidcError("id_token_no_kid", "ID token header missing 'kid'")

    jwks = await fetch_jwks(jwks_uri)
    matching_key = None
    for key in jwks["keys"]:
        if key.get("kid") == kid:
            matching_key = key
            break
    if matching_key is None:
        raise OidcError(
            "id_token_unknown_kid", f"no JWKS key matches kid='{kid}'"
        )

    try:
        claims = jwt.decode(
            id_token,
            matching_key,
            algorithms=[matching_key.get("alg", "RS256")],
            issuer=issuer,
            audience=client_id,
            options={"verify_at_hash": access_token is not None},
            access_token=access_token,
        )
    except JWTError as exc:
        raise OidcError(
            "id_token_invalid", f"signature or claim check failed: {exc}"
        ) from exc

    if claims.get("nonce") != expected_nonce:
        raise OidcError(
            "id_token_nonce_mismatch", "ID token nonce doesn't match"
        )

    return OidcClaims(
        sub=str(claims.get("sub", "")),
        issuer=str(claims.get("iss", "")),
        audience=claims.get("aud"),
        nonce=claims.get("nonce"),
        email=claims.get("email"),
        email_verified=claims.get("email_verified"),
        name=claims.get("name"),
        preferred_username=claims.get("preferred_username"),
        raw=claims,
    )


# ---------------------------------------------------------------------
# Userinfo (optional, for richer claim extraction)
# ---------------------------------------------------------------------


async def fetch_userinfo(
    *, userinfo_endpoint: str, access_token: str
) -> dict[str, Any]:
    """Hit the /userinfo endpoint with the access_token.

    Best-effort: returns {} on any error.  Caller should fall back
    to ID-token claims when this fails.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(
                userinfo_endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("userinfo fetch failed: %s", exc)
        return {}

    if resp.status_code != 200:
        logger.warning(
            "userinfo endpoint returned HTTP %s: %s",
            resp.status_code,
            resp.text[:200],
        )
        return {}

    try:
        return resp.json()
    except json.JSONDecodeError:
        return {}


__all__ = [
    "OidcClaims",
    "OidcDiscovery",
    "OidcError",
    "OidcTokenResponse",
    "build_authorization_url",
    "clear_discovery_cache",
    "clear_jwks_cache",
    "exchange_code_for_tokens",
    "fetch_discovery",
    "fetch_jwks",
    "fetch_userinfo",
    "generate_pkce_pair",
    "verify_id_token",
]
