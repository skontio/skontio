"""Persönliche Zugangstokens (skt_…) und die stdio-Bridge skontio-mcp."""

from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error
import urllib.request

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.utils import timezone

from skontio.audit.models import AuditEvent
from skontio.mcp.bridge import Bridge, endpoint
from skontio.mcp.models import PersonalAccessToken
from skontio.mcp.tokens import TokenError, authenticate_pat, create_token, revoke_token

MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
}


def post(client, raw, method, params=None, *, modern=True):
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})}
    headers = {"Authorization": f"Bearer {raw}", "Mcp-Method": method}
    if modern:
        message["params"]["_meta"] = META
        headers["MCP-Protocol-Version"] = MODERN
    if method == "tools/call":
        headers["Mcp-Name"] = message["params"]["name"]
    return client.post("/mcp", message, content_type="application/json", headers=headers)


class TestCreate:
    def test_token_is_shown_once_and_stored_as_hash(self, shop):
        token, raw = create_token(shop.user, name="Claude Desktop", scopes=["drafts:read"], days=30)
        assert raw.startswith("skt_") and len(raw) > 40
        assert token.prefix == raw[:12]
        assert raw not in token.token_hash and len(token.token_hash) == 64
        assert dt.timedelta(days=29) < token.expires_at - timezone.now() <= dt.timedelta(days=30)
        assert AuditEvent.objects.filter(action="mcp.pat.create", entity_id=str(token.pk)).exists()

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"name": " ", "scopes": ["drafts:read"]}, "Namen"),
            ({"name": "x", "scopes": []}, "Mindestens"),
            ({"name": "x", "scopes": ["admin"]}, "Unbekannte"),
            ({"name": "x", "scopes": ["drafts:read"], "days": 91}, "90"),
            ({"name": "x", "scopes": ["drafts:read"], "days": 0}, "90"),
        ],
    )
    def test_rejects_invalid_requests(self, shop, kwargs, message):
        with pytest.raises(TokenError, match=message):
            create_token(shop.user, **kwargs)

    def test_only_administrators(self, shop):
        user = get_user_model().objects.create_user("mitarbeiter")
        with pytest.raises(TokenError, match="Administratoren"):
            create_token(user, name="x", scopes=["drafts:read"])


class TestAuthenticate:
    def test_valid_token(self, shop):
        token, raw = create_token(shop.user, name="x", scopes=["drafts:read"])
        assert authenticate_pat(raw) == token
        token.refresh_from_db()
        assert token.last_used_at is not None

    def test_expired_revoked_unknown_and_inactive_are_rejected(self, shop):
        token, raw = create_token(shop.user, name="x", scopes=["drafts:read"])
        assert authenticate_pat(raw + "x") is None
        PersonalAccessToken.objects.filter(pk=token.pk).update(expires_at=timezone.now())
        assert authenticate_pat(raw) is None

        token, raw = create_token(shop.user, name="y", scopes=["drafts:read"])
        revoke_token(token, actor=shop.user)
        assert authenticate_pat(raw) is None

        token, raw = create_token(shop.user, name="z", scopes=["drafts:read"])
        shop.user.is_active = False
        shop.user.save()
        assert authenticate_pat(raw) is None


class TestEndpoint:
    def test_tools_follow_token_scopes(self, client, shop):
        _, raw = create_token(shop.user, name="x", scopes=["catalog:read"])
        names = {t["name"] for t in post(client, raw, "tools/list").json()["result"]["tools"]}
        assert "catalog_search" in names
        assert "invoice_draft_create" not in names

    def test_tool_call_is_audited_with_token_prefix(self, client, shop):
        token, raw = create_token(shop.user, name="x", scopes=["catalog:read"])
        response = post(client, raw, "tools/call", {"name": "catalog_search", "arguments": {"query": "Beratung"}})
        assert response.status_code == 200
        assert response.json()["result"]["isError"] is False
        event = AuditEvent.objects.filter(tool_name="catalog_search").latest("seq")
        assert event.client_id == f"pat:{token.prefix}"

    def test_legacy_clients_work_too(self, client, shop):
        _, raw = create_token(shop.user, name="x", scopes=["catalog:read"])
        assert post(client, raw, "tools/list", modern=False).json()["result"]["tools"]

    def test_revoked_token_gets_401(self, client, shop):
        token, raw = create_token(shop.user, name="x", scopes=["catalog:read"])
        revoke_token(token, actor=shop.user)
        assert post(client, raw, "tools/list").status_code == 401

    def test_missing_scope_is_403(self, client, shop):
        _, raw = create_token(shop.user, name="x", scopes=["catalog:read"])
        response = post(client, raw, "tools/call", {"name": "customer_search", "arguments": {"query": "K"}})
        assert response.status_code == 403
        assert response.json()["scope"] == "customers:read"

    def test_rate_limit_is_per_token(self, client, shop, settings):
        settings.MCP_RATE_LIMIT_PER_MINUTE = 2
        _, first = create_token(shop.user, name="a", scopes=["catalog:read"])
        _, second = create_token(shop.user, name="b", scopes=["catalog:read"])
        assert [post(client, first, "ping").status_code for _ in range(3)] == [200, 200, 429]
        assert post(client, second, "ping").status_code == 200


class TestWeb:
    def test_create_shows_token_once_with_snippets(self, client, shop):
        client.force_login(shop.user)
        response = client.post(
            "/einstellungen/ki-zugriff/tokens/",
            {"name": "Claude Desktop", "days": "7", "scopes": ["drafts:read", "drafts:write"]},
        )
        assert response.status_code == 200
        assert response["Cache-Control"] == "no-store"
        token = PersonalAccessToken.objects.get()
        raw = response.context["raw"]
        assert authenticate_pat(raw) == token
        body = response.content.decode()
        assert raw in body and "claude mcp add --transport http" in body
        page = client.get("/einstellungen/ki-zugriff/").content.decode()
        assert "Claude Desktop" in page and token.prefix in page and raw not in page

    def test_invalid_input_is_reported(self, client, shop):
        client.force_login(shop.user)
        response = client.post("/einstellungen/ki-zugriff/tokens/", {"name": "x", "days": "abc"}, follow=True)
        assert "Ungültige Gültigkeitsdauer" in response.content.decode()
        assert not PersonalAccessToken.objects.exists()

    def test_revoke(self, client, shop):
        token, raw = create_token(shop.user, name="x", scopes=["drafts:read"])
        client.force_login(shop.user)
        client.post(f"/einstellungen/ki-zugriff/tokens/{token.pk}/widerrufen/")
        assert authenticate_pat(raw) is None
        assert AuditEvent.objects.filter(action="mcp.pat.revoke").exists()

    def test_non_admins_cannot_create_tokens(self, client, shop):
        user = get_user_model().objects.create_user("mitarbeiter")
        client.force_login(user)
        response = client.post("/einstellungen/ki-zugriff/tokens/", {"name": "x", "scopes": ["drafts:read"]})
        assert response.status_code == 403

    def test_bridge_download(self, client, shop):
        client.force_login(shop.user)
        response = client.get("/einstellungen/ki-zugriff/skontio-mcp.py")
        assert response.status_code == 200
        assert 'filename="skontio-mcp.py"' in response["Content-Disposition"]
        assert b"import django" not in response.content and b"class Bridge" in response.content


class TestCommand:
    def test_create_list_revoke(self, shop):
        out = io.StringIO()
        call_command("mcp_token", "create", "inhaber", "--name", "CLI", "--scope", "drafts:read", stdout=out)
        raw = next(line for line in out.getvalue().splitlines() if line.startswith("skt_"))
        assert authenticate_pat(raw)
        out = io.StringIO()
        call_command("mcp_token", "list", stdout=out)
        assert raw[:12] in out.getvalue() and raw not in out.getvalue()
        call_command("mcp_token", "revoke", raw[:12], stdout=io.StringIO())
        assert authenticate_pat(raw) is None

    def test_unknown_user(self, db):
        with pytest.raises(CommandError, match="nicht gefunden"):
            call_command("mcp_token", "create", "niemand", "--name", "x", "--scope", "drafts:read")


# --- bridge -----------------------------------------------------------------------------------


class TestBridgeUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://skontio.example.de", "https://skontio.example.de/mcp"),
            ("https://skontio.example.de/", "https://skontio.example.de/mcp"),
            ("https://skontio.example.de/mcp", "https://skontio.example.de/mcp"),
            ("http://127.0.0.1:8000", "http://127.0.0.1:8000/mcp"),
            ("http://localhost:8000/", "http://localhost:8000/mcp"),
        ],
    )
    def test_endpoint(self, url, expected):
        assert endpoint(url) == expected

    @pytest.mark.parametrize("url", ["http://nas.local:8000", "ftp://x", "", "skontio.example.de"])
    def test_rejects_unsafe_or_invalid_urls(self, url):
        with pytest.raises(ValueError, match="http"):
            endpoint(url)

    def test_http_in_lan_only_when_asked(self):
        assert endpoint("http://nas.local:8000", allow_http=True) == "http://nas.local:8000/mcp"


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes):
        super().__init__(body)
        self.status = status


class DjangoOpener:
    """Routes the bridge's urllib requests into the Django test client."""

    def __init__(self, client):
        self.client = client
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        headers = {k: v for k, v in request.header_items() if k.lower() != "content-type"}
        response = self.client.post("/mcp", request.data, content_type="application/json", headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "", {}, io.BytesIO(response.content))
        return FakeResponse(response.status_code, response.content)


def run(bridge: Bridge, *messages) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    bridge.run(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


class TestBridge:
    def test_legacy_session_over_stdio(self, client, shop):
        _, raw = create_token(shop.user, name="Desktop", scopes=["catalog:read"])
        opener = DjangoOpener(client)
        bridge = Bridge("http://testserver/mcp", raw, opener=opener)
        replies = run(
            bridge,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "catalog_search", "arguments": {}}},
        )
        assert [r["id"] for r in replies] == [1, 2, 3]  # the notification gets no answer
        assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
        assert replies[2]["result"]["isError"] is False
        assert opener.requests[2].get_header("Mcp-protocol-version") == "2025-06-18"
        assert opener.requests[3].get_header("Mcp-name") == "catalog_search"

    def test_modern_messages_keep_their_meta_version(self, client, shop):
        _, raw = create_token(shop.user, name="Desktop", scopes=["catalog:read"])
        bridge = Bridge("http://testserver/mcp", raw, opener=DjangoOpener(client))
        message = {"jsonrpc": "2.0", "id": "a", "method": "server/discover", "params": {"_meta": META}}
        (reply,) = run(bridge, message)
        assert reply["result"]["resultType"] == "complete"

    def test_rejected_token_becomes_a_readable_error(self, client, shop):
        bridge = Bridge("http://testserver/mcp", "skt_falsch", opener=DjangoOpener(client))
        (reply,) = run(bridge, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        assert reply["id"] == 5
        assert "Zugangstoken abgelehnt" in reply["error"]["message"]

    def test_missing_scope_names_the_scope(self, client, shop):
        _, raw = create_token(shop.user, name="Desktop", scopes=["catalog:read"])
        bridge = Bridge("http://testserver/mcp", raw, opener=DjangoOpener(client))
        call = {"name": "customer_search", "arguments": {"query": "K"}}
        (reply,) = run(bridge, {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": call})
        assert "customers:read" in reply["error"]["message"]

    def test_non_ascii_tool_name_is_base64_encoded(self):
        bridge = Bridge("https://x/mcp", "skt_x")
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "prüfen"}}
        assert bridge.headers_for(message)["Mcp-Name"] == "=?base64?cHLDvGZlbg==?="

    def test_unreachable_server(self):
        def opener(request, timeout):
            raise urllib.error.URLError("connection refused")

        (reply,) = run(Bridge("https://x/mcp", "skt_x", opener=opener), {"jsonrpc": "2.0", "id": 9, "method": "ping"})
        assert reply["id"] == 9 and "nicht erreichbar" in reply["error"]["message"]

    def test_garbage_input_and_client_responses(self):
        bridge = Bridge("https://x/mcp", "skt_x", opener=lambda *a, **k: pytest.fail("no request expected"))
        assert json.loads(bridge.forward("{kaputt"))["error"]["code"] == -32700
        assert bridge.forward(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})) is None
