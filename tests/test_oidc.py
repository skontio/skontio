"""Login über einen OIDC-Provider (z. B. Pocket ID) und lokaler Notfall-Login nur aus dem LAN."""

from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from skontio.accounts import oidc
from skontio.accounts.models import OidcIdentity
from skontio.core.net import client_ip

ISSUER = "https://id.example.org"
CLIENT_ID = "skontio"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/api/oidc/token",
    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
    "end_session_endpoint": f"{ISSUER}/api/oidc/end-session",
    "code_challenge_methods_supported": ["plain", "S256"],
    "id_token_signing_alg_values_supported": ["RS256"],
}


@pytest.fixture(autouse=True)
def oidc_settings(settings):
    settings.OIDC_ISSUER = ISSUER
    settings.OIDC_CLIENT_ID = CLIENT_ID
    settings.OIDC_CLIENT_SECRET = "secret"
    settings.OIDC_REQUIRED_GROUP = "skontio"
    settings.OIDC_ADMIN_GROUP = "skontio-admin"
    oidc.clear_cache()


class FakeProvider:
    def __init__(self):
        self.challenge = None
        self.nonce = None
        self.claims_override: dict = {}
        self.signing_key = KEY

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=DISCOVERY)
        if path == "/.well-known/jwks.json":
            return httpx.Response(200, json=KeySet([KEY]).as_dict(private=False))
        if path == "/api/oidc/token":
            form = parse_qs(request.content.decode())
            verifier = form["code_verifier"][0]
            expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if expected != self.challenge or form["code"][0] != "good-code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": "user-123",
                "exp": now + 300,
                "iat": now,
                "nonce": self.nonce,
                "email": "max@example.org",
                "name": "Max Muster",
                "groups": ["skontio"],
            } | self.claims_override
            id_token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, self.signing_key)
            return httpx.Response(200, json={"access_token": "at", "id_token": id_token, "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(oidc, "http_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return fake


def start_login(client, provider, next_url="/dokumente/"):
    response = client.get("/auth/login", {"next": next_url})
    assert response.status_code == 302
    params = parse_qs(urlparse(response["Location"]).query)
    provider.challenge = params["code_challenge"][0]
    provider.nonce = params["nonce"][0]
    return params


def finish_login(client, params, code="good-code"):
    return client.get("/auth/callback", {"code": code, "state": params["state"][0]})


@pytest.mark.django_db
class TestOidcLogin:
    def test_authorization_request_uses_pkce_s256_state_and_nonce(self, client, provider):
        params = start_login(client, provider)
        assert params["code_challenge_method"] == ["S256"]
        assert params["response_type"] == ["code"]
        assert params["client_id"] == [CLIENT_ID]
        assert "groups" in params["scope"][0].split()
        assert len(params["state"][0]) >= 32 and len(params["nonce"][0]) >= 32

    def test_happy_path_creates_user_linked_by_issuer_and_subject(self, client, provider):
        response = finish_login(client, start_login(client, provider))
        assert response.status_code == 302 and response["Location"] == "/dokumente/"
        identity = OidcIdentity.objects.get(issuer=ISSUER, subject="user-123")
        assert identity.user.email == "max@example.org"
        assert not identity.user.has_usable_password()
        assert client.session["_auth_user_id"] == str(identity.user.pk)

    def test_returning_user_is_the_same_account(self, client, provider):
        finish_login(client, start_login(client, provider))
        client.logout()
        provider.claims_override = {"email": "neu@example.org"}
        finish_login(client, start_login(client, provider))
        assert get_user_model().objects.count() == 1
        assert OidcIdentity.objects.get().user.email == "neu@example.org"

    def test_same_email_with_other_subject_is_never_merged(self, client, provider):
        finish_login(client, start_login(client, provider))
        client.logout()
        provider.claims_override = {"sub": "attacker"}
        finish_login(client, start_login(client, provider))
        assert get_user_model().objects.count() == 2

    def test_state_mismatch_is_rejected(self, client, provider):
        start_login(client, provider)
        response = client.get("/auth/callback", {"code": "good-code", "state": "forged"})
        assert response.status_code == 400
        assert "_auth_user_id" not in client.session

    def test_callback_without_login_session_is_rejected(self, client, provider):
        assert client.get("/auth/callback", {"code": "good-code", "state": "x"}).status_code == 400

    @pytest.mark.parametrize(
        "override",
        [
            {"nonce": "wrong"},
            {"aud": "other-client"},
            {"iss": "https://evil.example"},
            {"exp": int(time.time()) - 3600},
        ],
    )
    def test_invalid_id_token_claims_are_rejected(self, client, provider, override):
        provider.claims_override = override
        response = finish_login(client, start_login(client, provider))
        assert response.status_code == 400
        assert not get_user_model().objects.exists()

    def test_token_signed_with_unknown_key_is_rejected(self, client, provider):
        provider.signing_key = OTHER_KEY
        assert finish_login(client, start_login(client, provider)).status_code == 400

    def test_wrong_code_is_rejected(self, client, provider):
        assert finish_login(client, start_login(client, provider), code="bad").status_code == 400

    def test_missing_required_group_is_forbidden(self, client, provider):
        provider.claims_override = {"groups": ["andere"]}
        response = finish_login(client, start_login(client, provider))
        assert response.status_code == 403
        assert not get_user_model().objects.exists()

    def test_admin_group_grants_staff(self, client, provider):
        provider.claims_override = {"groups": ["skontio", "skontio-admin"]}
        finish_login(client, start_login(client, provider))
        assert OidcIdentity.objects.get().user.is_staff
        client.logout()
        provider.claims_override = {"groups": ["skontio"]}
        finish_login(client, start_login(client, provider))
        assert not OidcIdentity.objects.get().user.is_staff  # revoked when the group is removed

    def test_open_redirect_is_prevented(self, client, provider):
        response = finish_login(client, start_login(client, provider, next_url="https://evil.example/"))
        assert response["Location"] == "/"

    def test_logout_redirects_to_end_session_endpoint(self, client, provider):
        finish_login(client, start_login(client, provider))
        response = client.post("/auth/logout")
        assert response.status_code == 302
        target = urlparse(response["Location"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == DISCOVERY["end_session_endpoint"]
        assert "id_token_hint" in parse_qs(target.query)
        assert "_auth_user_id" not in client.session


@pytest.mark.django_db
class TestLocalLogin:
    def test_blocked_from_public_addresses(self, client):
        assert client.get("/auth/local/", REMOTE_ADDR="203.0.113.7").status_code == 403

    def test_allowed_from_lan(self, client, django_user_model, settings):
        settings.LOCAL_LOGIN_REQUIRE_2FA = False  # second factor: tests/test_twofactor.py
        django_user_model.objects.create_user("notfall", password="ein-langes-passwort-123")
        response = client.post(
            "/auth/local/",
            {"username": "notfall", "password": "ein-langes-passwort-123"},
            REMOTE_ADDR="192.168.1.20",
        )
        assert response.status_code == 302

    def test_public_client_behind_trusted_proxy_is_blocked(self, client, settings):
        settings.TRUSTED_PROXIES = ["172.16.0.0/12"]
        response = client.get("/auth/local/", REMOTE_ADDR="172.18.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")
        assert response.status_code == 403


class TestClientIp:
    def test_forwarded_header_from_untrusted_peer_is_ignored(self, settings):
        settings.TRUSTED_PROXIES = ["172.16.0.0/12"]
        request = RequestFactory().get("/", REMOTE_ADDR="203.0.113.7", HTTP_X_FORWARDED_FOR="192.168.1.5")
        assert str(client_ip(request)) == "203.0.113.7"

    def test_rightmost_untrusted_address_is_the_client(self, settings):
        settings.TRUSTED_PROXIES = ["172.16.0.0/12"]
        request = RequestFactory().get(
            "/", REMOTE_ADDR="172.18.0.1", HTTP_X_FORWARDED_FOR="10.9.9.9, 198.51.100.4, 172.18.0.5"
        )
        assert str(client_ip(request)) == "198.51.100.4"
