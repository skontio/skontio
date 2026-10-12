"""Zustimmung für MCP-Clients (nur Admins, deutsch), vorab registrierte Clients, Widerruf."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
from io import StringIO
from urllib.parse import parse_qs, urlparse

import pytest
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, RefreshToken

from skontio.audit.models import AuditEvent

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def claude(shop):
    out = StringIO()
    call_command("mcp_client", "--name", "Claude", "--redirect-uri", REDIRECT, stdout=out)
    return Application.objects.get(name="Claude")


def authorize_params(application):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return {
        "response_type": "code",
        "client_id": application.client_id,
        "redirect_uri": REDIRECT,
        "scope": "catalog:read drafts:write",
        "state": "s1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": f"{settings.PUBLIC_URL}/mcp",
    }


def test_registered_client_is_public_with_pkce(claude):
    assert claude.client_type == Application.CLIENT_PUBLIC and claude.redirect_uris == REDIRECT


def test_client_command_rejects_plain_http():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("mcp_client", "--name", "X", "--redirect-uri", "http://evil.example/cb")


def test_consent_page_is_german_and_explains_limits(client, shop, claude):
    client.force_login(shop.user)
    body = client.get("/oauth/authorize/", authorize_params(claude)).content.decode()
    assert "Claude Zugriff auf Skontio erlauben?" in body
    assert "claude.ai" in body and "Leistungskatalog lesen" in body
    assert "Festschreiben, Versenden" in body and "pseudonymisiert" in body


def test_only_admins_may_consent(client, shop, claude, django_user_model):
    client.force_login(django_user_model.objects.create_user("mitarbeiter"))
    assert client.get("/oauth/authorize/", authorize_params(claude)).status_code == 403


def test_consent_issues_code_and_is_audited(client, shop, claude):
    client.force_login(shop.user)
    response = client.post("/oauth/authorize/", {**authorize_params(claude), "allow": "Authorize"})
    assert response.status_code == 302
    assert parse_qs(urlparse(response["Location"]).query)["state"] == ["s1"]
    assert AuditEvent.objects.filter(action="mcp.consent", actor=shop.user).exists()


def test_cimd_is_advertised_and_dcr_is_off(client):
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["client_id_metadata_document_supported"] is True
    assert "registration_endpoint" not in metadata


def test_revoke_removes_all_tokens(client, shop, claude):
    access = AccessToken.objects.create(
        user=shop.user,
        application=claude,
        token=secrets.token_urlsafe(),
        scope="catalog:read",
        expires=timezone.now() + dt.timedelta(minutes=15),
    )
    RefreshToken.objects.create(user=shop.user, application=claude, token=secrets.token_urlsafe(), access_token=access)
    client.force_login(shop.user)
    page = client.get("/einstellungen/ki-zugriff/").content.decode()
    assert "Claude" in page and f"{settings.PUBLIC_URL}/mcp" in page
    response = client.post(f"/einstellungen/ki-zugriff/{claude.pk}/widerrufen/")
    assert response.status_code == 302
    assert not AccessToken.objects.exists()
    assert not RefreshToken.objects.filter(revoked__isnull=True).exists()
    assert AuditEvent.objects.filter(action="mcp.revoke").exists()
