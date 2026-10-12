"""Bearer-token gate for /mcp (MCP authorization spec 2026-07-28).

* Only tokens issued by Skontio's own authorization server are accepted (no passthrough).
* The token audience must be exactly the MCP resource (RFC 8707). django-oauth-toolkit
  treats tokens without ``resource`` as unrestricted – Skontio rejects them here.
* Session cookies never authenticate MCP requests.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from oauth2_provider.models import get_access_token_model
from oauth2_provider.www_authenticate import build_bearer_challenge

from skontio.audit.models import AuditEvent
from skontio.mcp.protocol import Reply, handle_message
from skontio.mcp.tokens import TOKEN_PREFIX, authenticate_pat
from skontio.tools.registry import ToolContext
from skontio.web.support import current_company


def mcp_resource() -> str:
    return f"{settings.PUBLIC_URL}/mcp"


def _normalise(uri: str) -> str:
    return uri.rstrip("/")


def resource_matches_exactly(request_uri: str, audiences: list[str]) -> bool:
    """RFC 8707 validator: exact match instead of django-oauth-toolkit's URL-prefix default."""
    return _normalise(request_uri) in {_normalise(a) for a in audiences if isinstance(a, str)}


@dataclass(frozen=True)
class Principal:
    """Who calls /mcp: an OAuth access token or a personal access token (``skt_…``)."""

    user: object
    scopes: frozenset[str]
    client_id: str
    rate_key: str


def authenticate_mcp(request: HttpRequest) -> Principal | None:
    header = request.headers.get("Authorization", "")
    scheme, _, raw_token = header.partition(" ")
    raw_token = raw_token.strip()
    if scheme.lower() != "bearer" or not raw_token:
        return None
    if raw_token.startswith(TOKEN_PREFIX):
        pat = authenticate_pat(raw_token)
        if pat is None:
            return None
        return Principal(pat.user, frozenset(pat.scope_list), f"pat:{pat.prefix}", f"pat:{pat.pk}")
    checksum = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    tokens = get_access_token_model().objects.select_related("user", "application")
    token = tokens.filter(token_checksum=checksum).first()
    if token is None or not token.is_valid():
        return None
    if not token.resource or not resource_matches_exactly(mcp_resource(), token.resource):
        return None
    client_id = token.application.client_id if token.application else ""
    return Principal(token.user, frozenset(token.scope.split()), client_id, f"oauth:{token.pk}")


def _unauthorized(request: HttpRequest) -> JsonResponse:
    response = JsonResponse({"error": "invalid_token"}, status=401)
    response["WWW-Authenticate"] = build_bearer_challenge(request, resource_metadata_url=resource_metadata_url())
    return response


def resource_metadata_url() -> str:
    return f"{settings.PUBLIC_URL}/.well-known/oauth-protected-resource"


def _origin_allowed(request: HttpRequest) -> bool:
    """DNS-rebinding protection (spec: validate Origin). Server-to-server clients send no Origin."""
    origin = request.headers.get("Origin")
    return origin is None or origin.rstrip("/") in {settings.PUBLIC_URL, *settings.MCP_ALLOWED_ORIGINS}


def _rate_limited(principal: Principal) -> bool:
    key = f"mcp-rate:{principal.rate_key}:{int(time.time() // 60)}"
    cache.add(key, 0, timeout=120)
    return cache.incr(key) > settings.MCP_RATE_LIMIT_PER_MINUTE


def _json(reply: Reply) -> HttpResponse:
    if reply.body is None:
        response = HttpResponse(status=reply.status)
    else:
        response = JsonResponse(reply.body, status=reply.status, json_dumps_params={"ensure_ascii": False})
    for name, value in reply.headers.items():
        response[name] = value
    return response


@csrf_exempt  # bearer-token API, no cookies
def mcp_endpoint(request: HttpRequest) -> HttpResponse:
    """Streamable HTTP MCP endpoint (POST only; see skontio.mcp.protocol)."""
    principal = authenticate_mcp(request)
    if principal is None:
        return _unauthorized(request)
    if not _origin_allowed(request):
        return JsonResponse({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Origin not allowed"}}, status=403)
    if request.method != "POST":
        response = HttpResponse(status=405)  # no GET stream, no sessions (2026-07-28)
        response["Allow"] = "POST"
        return response
    if _rate_limited(principal):
        response = JsonResponse({"error": "rate_limited"}, status=429)
        response["Retry-After"] = "60"
        return response
    company = current_company()
    if company is None or not principal.user.is_active:
        return JsonResponse({"error": "Skontio ist noch nicht eingerichtet"}, status=503)

    ctx = ToolContext(
        company=company,
        user=principal.user,
        scopes=principal.scopes & frozenset(settings.MCP_SCOPES),
        actor_kind=AuditEvent.ActorKind.MCP,
        client_id=principal.client_id,
        pseudonymise_persons=settings.MCP_PSEUDONYMISE_PERSONS,
    )

    def insufficient_scope(scope: str) -> Reply:
        challenge = build_bearer_challenge(
            request,
            oauth2_error={"error": "insufficient_scope", "scope": scope},
            resource_metadata_url=resource_metadata_url(),
        )
        body = {"error": "insufficient_scope", "scope": scope}
        return Reply(403, body, {"WWW-Authenticate": challenge})

    return _json(handle_message(request.body, request.headers, ctx, insufficient_scope=insufficient_scope))
