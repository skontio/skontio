"""Phase-0 spike: MCP authorization per spec 2026-07-28 with django-oauth-toolkit.

Authorization code + PKCE + RFC 8707 resource indicator; /mcp only accepts tokens whose
audience is exactly the MCP resource (no "unrestricted" tokens); RFC 9728 metadata.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application

MCP_RESOURCE = f"{settings.PUBLIC_URL}/mcp"
REDIRECT_URI = "http://127.0.0.1:33418/callback"


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("inhaber", password=secrets.token_urlsafe(), is_staff=True)


@pytest.fixture
def application(user):
    return Application.objects.create(
        name="Claude",
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=REDIRECT_URI,
        user=user,
        skip_authorization=False,
    )


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def obtain_token(client, application, user, *, resource: str | None) -> dict:
    client.force_login(user)
    verifier, challenge = pkce_pair()
    params = {
        "response_type": "code",
        "client_id": application.client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": "catalog:read drafts:write",
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if resource:
        params["resource"] = resource
    response = client.post("/oauth/authorize/", {**params, "allow": "Authorize"})
    assert response.status_code == 302, response.content
    query = parse_qs(urlparse(response["Location"]).query)
    assert query["state"] == ["xyz"]
    token_request = {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "redirect_uri": REDIRECT_URI,
        "client_id": application.client_id,
        "code_verifier": verifier,
    }
    if resource:
        token_request["resource"] = resource
    token_response = client.post("/oauth/token/", token_request)
    assert token_response.status_code == 200, token_response.content
    return token_response.json()


@pytest.mark.django_db
class TestAuthorizationServer:
    def test_pkce_is_mandatory(self, client, application, user):
        client.force_login(user)
        response = client.get(
            "/oauth/authorize/",
            {"response_type": "code", "client_id": application.client_id, "redirect_uri": REDIRECT_URI},
        )
        location = response.get("Location", "")
        assert "error=invalid_request" in location or response.status_code == 400

    def test_token_is_bound_to_mcp_resource(self, client, application, user):
        tokens = obtain_token(client, application, user, resource=MCP_RESOURCE)
        stored = AccessToken.objects.get(token_checksum=hashlib.sha256(tokens["access_token"].encode()).hexdigest())
        assert stored.resource == [MCP_RESOURCE]
        assert tokens["expires_in"] <= 900
        assert "refresh_token" in tokens

    def test_protected_resource_metadata(self, client):
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        body = response.json()
        assert body["resource"] == MCP_RESOURCE
        assert body["authorization_servers"] == [settings.PUBLIC_URL]

    def test_authorization_server_metadata(self, client):
        body = client.get("/.well-known/oauth-authorization-server").json()
        assert "S256" in body["code_challenge_methods_supported"]
        assert body["issuer"] == settings.PUBLIC_URL


@pytest.mark.django_db
class TestMcpEndpoint:
    def test_missing_token_gets_challenge_with_resource_metadata(self, client):
        response = client.post("/mcp", content_type="application/json", data={})
        assert response.status_code == 401
        challenge = response["WWW-Authenticate"]
        assert challenge.startswith("Bearer")
        assert "resource_metadata=" in challenge

    def test_valid_token_is_accepted(self, client, application, user):
        from skontio.core.models import Company

        Company.objects.create(name="Firma")
        tokens = obtain_token(client, application, user, resource=MCP_RESOURCE)
        client.logout()
        response = client.post(
            "/mcp",
            content_type="application/json",
            data={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}",
        )
        assert response.status_code == 200
        assert response.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_token_without_resource_is_rejected(self, client, application, user):
        tokens = obtain_token(client, application, user, resource=None)
        client.logout()
        response = client.post(
            "/mcp", content_type="application/json", data={}, HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}"
        )
        assert response.status_code == 401

    def test_token_for_other_resource_is_rejected(self, client, application, user):
        tokens = obtain_token(client, application, user, resource="https://other.example/mcp")
        client.logout()
        response = client.post(
            "/mcp", content_type="application/json", data={}, HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}"
        )
        assert response.status_code == 401

    def test_expired_token_is_rejected(self, client, application, user):
        tokens = obtain_token(client, application, user, resource=MCP_RESOURCE)
        AccessToken.objects.update(expires=timezone.now() - timedelta(seconds=1))
        client.logout()
        response = client.post(
            "/mcp", content_type="application/json", data={}, HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}"
        )
        assert response.status_code == 401

    def test_session_cookie_alone_is_not_enough(self, client, user):
        client.force_login(user)
        assert client.post("/mcp", content_type="application/json", data={}).status_code == 401
