"""Minimal OpenID Connect relying party (authorization code + PKCE S256).

Written against httpx so it can be tested with a mocked provider. Security choices:
* PKCE S256 always (Pocket ID also offers "plain" – never used)
* state and nonce: 256-bit random, compared in constant time
* ID token: signature from the provider's JWKS, asymmetric algorithms only,
  iss/aud/exp/iat/nonce validated with 60 s leeway
* users are linked by issuer + subject, never by e-mail address
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from skontio.accounts.models import OidcIdentity

SCOPES = "openid profile email groups"
ALLOWED_ALGORITHMS = {"RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA"}
CLOCK_LEEWAY_SECONDS = 60
DISCOVERY_TTL_SECONDS = 3600


class OidcError(Exception):
    """Authentication failed – show a generic error, log the detail."""


class OidcForbidden(OidcError):
    """Authenticated at the provider, but not allowed to use Skontio."""


def http_client_factory() -> httpx.Client:
    return httpx.Client(timeout=10, follow_redirects=False)


_cache: dict[str, tuple[float, dict]] = {}


def clear_cache() -> None:
    _cache.clear()


def _get_json(url: str) -> dict:
    with http_client_factory() as http:
        response = http.get(url)
        response.raise_for_status()
        return response.json()


def discover(issuer: str) -> dict:
    cached = _cache.get(issuer)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        discovery = _get_json(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"Discovery von {issuer} fehlgeschlagen: {exc}") from exc
    if discovery.get("issuer") != issuer:
        raise OidcError("Issuer der Discovery stimmt nicht mit SKONTIO_OIDC_ISSUER überein")
    if "S256" not in discovery.get("code_challenge_methods_supported", ["S256"]):
        raise OidcError("Der Provider unterstützt kein PKCE S256")
    _cache[issuer] = (time.monotonic() + DISCOVERY_TTL_SECONDS, discovery)
    return discovery


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str
    nonce: str
    code_verifier: str


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def authorization_request(redirect_uri: str) -> AuthorizationRequest:
    discovery = discover(settings.OIDC_ISSUER)
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.OIDC_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return AuthorizationRequest(f"{discovery['authorization_endpoint']}?{query}", state, nonce, verifier)


def states_match(expected: str, received: str) -> bool:
    return bool(expected) and hmac.compare_digest(expected.encode(), received.encode())


def exchange_code(code: str, *, code_verifier: str, redirect_uri: str) -> dict:
    discovery = discover(settings.OIDC_ISSUER)
    try:
        with http_client_factory() as http:
            response = http.post(
                discovery["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                    "client_id": settings.OIDC_CLIENT_ID,
                },
                auth=(settings.OIDC_CLIENT_ID, settings.OIDC_CLIENT_SECRET),  # client_secret_basic
            )
    except httpx.HTTPError as exc:
        raise OidcError(f"Token-Anfrage fehlgeschlagen: {exc}") from exc
    if response.status_code != 200:
        raise OidcError(f"Token-Anfrage abgelehnt ({response.status_code})")
    tokens = response.json()
    if "id_token" not in tokens:
        raise OidcError("Antwort enthält kein ID-Token")
    return tokens


def validate_id_token(id_token: str, *, nonce: str) -> dict:
    discovery = discover(settings.OIDC_ISSUER)
    algorithms = sorted(ALLOWED_ALGORITHMS & set(discovery.get("id_token_signing_alg_values_supported", ["RS256"])))
    if not algorithms:
        raise OidcError("Der Provider bietet keinen zulässigen Signaturalgorithmus an")
    try:
        keys = KeySet.import_key_set(_get_json(discovery["jwks_uri"]))
        token = jwt.decode(id_token, keys, algorithms=algorithms)
        registry = jwt.JWTClaimsRegistry(
            leeway=CLOCK_LEEWAY_SECONDS,
            iss={"essential": True, "value": settings.OIDC_ISSUER},
            aud={"essential": True, "value": settings.OIDC_CLIENT_ID},
            sub={"essential": True},
            exp={"essential": True},
            iat={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        registry.validate(token.claims)
    except (JoseError, ValueError, httpx.HTTPError) as exc:
        raise OidcError(f"ID-Token ungültig: {exc}") from exc
    claims = token.claims
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if len(audiences) > 1 and claims.get("azp") != settings.OIDC_CLIENT_ID:
        raise OidcError("ID-Token: azp passt nicht zu dieser Anwendung")
    return claims


def _groups(claims: dict) -> set[str]:
    groups = claims.get("groups") or []
    return {groups} if isinstance(groups, str) else set(groups)


def map_identity(claims: dict):
    """Find or create the local user for issuer+subject and apply group-based permissions."""
    groups = _groups(claims)
    if settings.OIDC_REQUIRED_GROUP and settings.OIDC_REQUIRED_GROUP not in groups:
        raise OidcForbidden(f"Mitgliedschaft in der Gruppe {settings.OIDC_REQUIRED_GROUP!r} fehlt")
    is_admin = bool(settings.OIDC_ADMIN_GROUP) and settings.OIDC_ADMIN_GROUP in groups
    issuer, subject = claims["iss"], claims["sub"]
    User = get_user_model()
    with transaction.atomic():
        identity = OidcIdentity.objects.select_related("user").filter(issuer=issuer, subject=subject).first()
        if identity is None:
            digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()[:20]
            user = User(username=f"oidc-{digest}")
            user.set_unusable_password()
            user.save()
            identity = OidcIdentity.objects.create(user=user, issuer=issuer, subject=subject)
        user = identity.user
        user.email = claims.get("email", "") or ""
        user.first_name = (claims.get("name") or claims.get("given_name") or "")[:150]
        user.is_staff = user.is_superuser = is_admin
        user.save(update_fields=["email", "first_name", "is_staff", "is_superuser"])
        identity.last_login_at = timezone.now()
        identity.save(update_fields=["last_login_at"])
    return user


def end_session_url(id_token: str, post_logout_redirect_uri: str) -> str | None:
    try:
        endpoint = discover(settings.OIDC_ISSUER).get("end_session_endpoint")
    except OidcError:
        return None
    if not endpoint:
        return None
    query = urlencode(
        {
            "id_token_hint": id_token,
            "post_logout_redirect_uri": post_logout_redirect_uri,
            "client_id": settings.OIDC_CLIENT_ID,
        }
    )
    return f"{endpoint}?{query}"
