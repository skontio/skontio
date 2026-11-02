"""Was aus dem Internet ohne Login erreichbar ist – exakt festgeschrieben.

Jede Route, die ohne Sitzung etwas anderes liefert als "zum Login", 401, 403, 404 oder 405, muss hier
mit Begründung stehen. Eine neue öffentliche Route lässt diesen Test scheitern, bis sie bewusst
geprüft und eingetragen ist. Dazu: Sicherheits-Header, Cookies, entfernte Endpunkte.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import URLResolver, get_resolver

# route pattern → why it is public (and where its behaviour is tested)
PUBLIC = {
    ".well-known/oauth-authorization-server": "RFC 8414 Metadaten für MCP-Clients (test_mcp_oauth)",
    ".well-known/oauth-authorization-server/<path:issuer_path>": "RFC 8414 mit Pfad (test_mcp_oauth)",
    ".well-known/oauth-protected-resource": "RFC 9728 Metadaten für /mcp (test_mcp_oauth)",
    ".well-known/oauth-protected-resource/<path:resource_path>": "RFC 9728 mit Pfad (test_mcp_oauth)",
    "oauth/token/": "Token-Ausgabe mit PKCE und Client-Prüfung (test_mcp_oauth)",
    "oauth/revoke_token/": "Token-Widerruf durch den Client (test_mcp_oauth)",
    "mcp": "Bearer-Token-API, Audience und Scopes geprüft (test_mcp_oauth, test_mcp_tokens)",
    "auth/login": "Start des Pocket-ID-Logins (test_oidc)",
    "auth/callback": "Rückkehr von Pocket ID, state/nonce/PKCE geprüft (test_oidc)",
    "auth/logout": "Abmelden (test_oidc)",
    "auth/local/": "Notfall-Login: nur LAN, Sperre, zweiter Faktor (test_adversarial, test_twofactor)",
    "auth/local/2fa/": "zweiter Faktor des Notfall-Logins (test_adversarial)",
    "manifest.webmanifest": "PWA-Manifest ohne Daten (test_pwa)",
    "sw.js": "Service Worker, cacht nur die App-Hülle (test_pwa)",
    "theme.css": "eigenes Theme, nur wenn eingerichtet (test_onboarding)",
}
NOT_FOUND = [
    "/admin/", "/admin/login/", "/oauth/introspect/", "/oauth/device-authorization/", "/oauth/device/",
    "/oauth/applications/", "/oauth/register/", "/oauth/userinfo/", "/.well-known/jwks.json",
]  # fmt: skip


def _routes() -> list[str]:
    def walk(resolver, prefix=""):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern, prefix + str(pattern.pattern))
            else:
                yield prefix + str(pattern.pattern)

    return sorted(set(walk(get_resolver())))


def _url(route: str) -> str:
    return "/" + re.sub(r"<(int|slug|str|path):[a-z_]+>", lambda m: "1" if m.group(1) == "int" else "x", route)


def _protected(response) -> bool:
    location = response.get("Location", "")
    if response.status_code in (301, 302) and location.startswith(("/auth/", "/einrichtung/")):
        return True
    return response.status_code in (401, 403, 404, 405)


@pytest.mark.django_db
def test_exactly_the_reviewed_routes_answer_without_login(shop):
    client = Client()
    answering = set()
    for route in _routes():
        url = _url(route)
        if not all(_protected(response) for response in (client.get(url), client.post(url))):
            answering.add(route)
    unexpected = answering - set(PUBLIC)
    assert unexpected == set(), f"neue öffentliche Routen – prüfen und in PUBLIC eintragen: {sorted(unexpected)}"
    missing = set(PUBLIC) - set(_routes())
    assert missing == set(), f"PUBLIC nennt Routen, die es nicht mehr gibt: {missing}"


@pytest.mark.django_db
@pytest.mark.parametrize("url", NOT_FOUND)
def test_removed_endpoints_stay_gone(client, url):
    assert client.get(url).status_code == 404
    assert client.post(url, {"username": "admin", "password": "admin"}).status_code == 404


@pytest.mark.django_db
def test_security_headers_on_public_and_private_pages(client, shop):
    for response in (client.get("/auth/local/"), client.get("/manifest.webmanifest")):
        assert response["X-Frame-Options"] == "DENY"
        assert response["X-Content-Type-Options"] == "nosniff"
    client.force_login(shop.user)
    page = client.get("/")
    csp = page["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert page["Referrer-Policy"] == "same-origin"


def test_session_cookie_flags(settings):
    """Lax keeps other sites from posting with the session (the share target relies on it)."""
    assert settings.SESSION_COOKIE_HTTPONLY and settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_SECURE == settings.SESSION_COOKIE_SECURE == settings.PUBLIC_URL.startswith("https://")


@pytest.mark.django_db
def test_healthz_reveals_nothing(client):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_unknown_host_is_refused(client):
    assert client.get("/auth/local/", HTTP_HOST="evil.example").status_code == 400


@pytest.mark.django_db
def test_debug_is_off_and_errors_do_not_leak(client, settings):
    assert settings.DEBUG is False
    response = client.get("/dokumente/999999999999999999999/")
    assert response.status_code in (302, 404) and b"Traceback" not in response.content
