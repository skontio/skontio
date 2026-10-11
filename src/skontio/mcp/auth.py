"""Bearer-token gate for /mcp (MCP authorization spec 2026-07-28).

* Only tokens issued by Skontio's own authorization server are accepted (no passthrough).
* The token audience must be exactly the MCP resource (RFC 8707). django-oauth-toolkit
  treats tokens without ``resource`` as unrestricted – Skontio rejects them here.
* Session cookies never authenticate MCP requests.
"""

from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from oauth2_provider.models import get_access_token_model
from oauth2_provider.www_authenticate import build_bearer_challenge


def mcp_resource() -> str:
    return f"{settings.PUBLIC_URL}/mcp"


def _normalise(uri: str) -> str:
    return uri.rstrip("/")


def resource_matches_exactly(request_uri: str, audiences: list[str]) -> bool:
    """RFC 8707 validator: exact match instead of django-oauth-toolkit's URL-prefix default."""
    return _normalise(request_uri) in {_normalise(a) for a in audiences if isinstance(a, str)}


def authenticate_mcp(request: HttpRequest):
    header = request.headers.get("Authorization", "")
    scheme, _, raw_token = header.partition(" ")
    if scheme.lower() != "bearer" or not raw_token.strip():
        return None
    checksum = hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()
    token = get_access_token_model().objects.select_related("user").filter(token_checksum=checksum).first()
    if token is None or not token.is_valid():
        return None
    if not token.resource or not resource_matches_exactly(mcp_resource(), token.resource):
        return None
    return token


def _unauthorized(request: HttpRequest) -> JsonResponse:
    response = JsonResponse({"error": "invalid_token"}, status=401)
    response["WWW-Authenticate"] = build_bearer_challenge(
        request, resource_metadata_url=f"{settings.PUBLIC_URL}/.well-known/oauth-protected-resource"
    )
    return response


@csrf_exempt  # bearer-token API, no cookies
@require_http_methods(["GET", "POST", "DELETE"])
def mcp_endpoint(request: HttpRequest) -> JsonResponse:
    """Placeholder until the Streamable HTTP transport is mounted in Phase 1b."""
    token = authenticate_mcp(request)
    if token is None:
        return _unauthorized(request)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)
    return JsonResponse({"status": "authorized", "scopes": sorted(token.scope.split()), "echo": bool(payload)})
