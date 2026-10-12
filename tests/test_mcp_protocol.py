"""MCP Streamable HTTP: modern (2026-07-28, zustandslos) und legacy (initialize), über echte Tokens."""

from __future__ import annotations

import base64
import datetime as dt
import secrets

import pytest
from django.conf import settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application

from skontio.documents.models import Document

MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture
def token(shop):
    application = Application.objects.create(
        name="Claude",
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://claude.ai/api/mcp/auth_callback",
        user=shop.user,
    )

    def make(scope="catalog:read customers:read drafts:read drafts:write"):
        raw = secrets.token_urlsafe(32)
        AccessToken.objects.create(
            user=shop.user,
            application=application,
            token=raw,
            expires=timezone.now() + dt.timedelta(minutes=15),
            scope=scope,
            resource=[f"{settings.PUBLIC_URL}/mcp"],
        )
        return raw

    return make


def modern(client, raw, method, params=None, *, headers=None, **extra):
    params = {**(params or {}), "_meta": META}
    default_headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if method == "tools/call":
        default_headers["Mcp-Name"] = params["name"]
    return client.post(
        "/mcp",
        {"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
        content_type="application/json",
        headers={"Authorization": f"Bearer {raw}", **default_headers, **(headers or {})},
        **extra,
    )


def legacy(client, raw, method, params=None, version="2025-06-18", **message):
    headers = {"Authorization": f"Bearer {raw}"}
    if version:
        headers["MCP-Protocol-Version"] = version
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}, **message}
    return client.post("/mcp", body, content_type="application/json", headers=headers)


class TestModern:
    def test_discover(self, client, token):
        result = modern(client, token(), "server/discover").json()["result"]
        assert result["resultType"] == "complete"
        assert result["supportedVersions"][0] == MODERN
        assert "tools" in result["capabilities"]
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "skontio"
        assert "untrusted_text" in result["instructions"]

    def test_tools_list_depends_on_granted_scopes(self, client, token):
        names = {t["name"] for t in modern(client, token("catalog:read"), "tools/list").json()["result"]["tools"]}
        assert names == {"catalog_get", "catalog_search"}

    def test_tool_call_returns_structured_and_text_content(self, client, token, shop):
        response = modern(client, token(), "tools/call", {"name": "catalog_search", "arguments": {"query": "Ber"}})
        result = response.json()["result"]
        assert response.status_code == 200 and result["isError"] is False
        assert result["structuredContent"]["items"][0]["name"] == "Beratung"
        assert '"Beratung"' in result["content"][0]["text"]

    def test_draft_created_over_http_is_marked_as_ai_draft(self, client, token, shop):
        arguments = {
            "customer_number": shop.org.number,
            "idempotency_key": "http-0001-xyz",
            "service_date": "2026-10-09",
            "lines": [{"item_id": shop.item.pk, "quantity": "2"}],
        }
        result = modern(client, token(), "tools/call", {"name": "invoice_draft_create", "arguments": arguments})
        document = Document.objects.get(pk=result.json()["result"]["structuredContent"]["document_id"])
        assert document.origin == Document.Origin.MCP and document.ai_review_required

    def test_tool_errors_are_results_the_model_can_fix(self, client, token):
        result = modern(client, token(), "tools/call", {"name": "catalog_get", "arguments": {"item_id": 4711}})
        body = result.json()["result"]
        assert result.status_code == 200 and body["isError"] is True and "catalog_search" in body["content"][0]["text"]

    def test_missing_scope_asks_for_step_up(self, client, token):
        response = modern(client, token("catalog:read"), "tools/call", {"name": "catalog_create", "arguments": {}})
        assert response.status_code == 403
        challenge = response["WWW-Authenticate"]
        assert 'error="insufficient_scope"' in challenge and 'scope="catalog:write"' in challenge
        assert "resource_metadata=" in challenge

    def test_unknown_tool_is_a_protocol_error(self, client, token):
        body = modern(client, token(), "tools/call", {"name": "invoice_finalize", "arguments": {}}).json()
        assert body["error"]["code"] == -32602

    @pytest.mark.parametrize(
        "headers",
        [
            {"MCP-Protocol-Version": "2025-06-18"},
            {"Mcp-Method": "tools/list"},
            {"Mcp-Name": "catalog_create"},
        ],
    )
    def test_headers_must_match_body(self, client, token, headers):
        response = modern(client, token(), "tools/call", {"name": "catalog_search", "arguments": {}}, headers=headers)
        assert response.status_code == 400 and response.json()["error"]["code"] == -32020

    def test_base64_encoded_tool_name_header(self, client, token):
        encoded = "=?base64?" + base64.b64encode(b"catalog_search").decode() + "?="
        response = modern(
            client, token(), "tools/call", {"name": "catalog_search", "arguments": {}}, headers={"Mcp-Name": encoded}
        )
        assert response.status_code == 200

    def test_unsupported_version_lists_supported_ones(self, client, token):
        response = client.post(
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {**META, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}},
            },
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {token()}",
                "MCP-Protocol-Version": "2099-01-01",
                "Mcp-Method": "tools/list",
            },
        )
        error = response.json()["error"]
        assert response.status_code == 400 and error["code"] == -32022
        assert MODERN in error["data"]["supported"]

    def test_missing_capabilities_meta_is_invalid_params(self, client, token):
        meta = {"io.modelcontextprotocol/protocolVersion": MODERN}
        response = client.post(
            "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}},
            content_type="application/json",
            headers={"Authorization": f"Bearer {token()}", "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"},
        )
        assert response.status_code == 400 and response.json()["error"]["code"] == -32602

    def test_unknown_method_is_404(self, client, token):
        response = modern(client, token(), "resources/list")
        assert response.status_code == 404 and response.json()["error"]["code"] == -32601

    def test_no_session_id_is_minted(self, client, token):
        assert "Mcp-Session-Id" not in modern(client, token(), "tools/list")


class TestLegacy:
    def test_initialize_negotiates_without_session(self, client, token):
        response = legacy(
            client,
            token(),
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}},
            version=None,
        )
        result = response.json()["result"]
        assert result["protocolVersion"] == "2025-06-18" and result["serverInfo"]["name"] == "skontio"
        assert "Mcp-Session-Id" not in response

    def test_unknown_requested_version_gets_latest_legacy(self, client, token):
        response = legacy(client, token(), "initialize", {"protocolVersion": "2024-11-05"}, version=None)
        assert response.json()["result"]["protocolVersion"] == "2025-11-25"

    def test_initialized_notification_is_accepted(self, client, token):
        response = client.post(
            "/mcp",
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            content_type="application/json",
            headers={"Authorization": f"Bearer {token()}", "MCP-Protocol-Version": "2025-06-18"},
        )
        assert response.status_code == 202 and response.content == b""

    def test_tools_list_and_call_without_meta(self, client, token):
        assert legacy(client, token(), "tools/list").json()["result"]["tools"]
        result = legacy(client, token(), "tools/call", {"name": "catalog_search", "arguments": {}}).json()["result"]
        assert "resultType" not in result and result["isError"] is False

    def test_missing_version_header_is_treated_as_oldest_legacy(self, client, token):
        assert legacy(client, token(), "ping", version=None).json()["result"] == {}


class TestTransport:
    def test_get_and_delete_are_not_allowed(self, client, token):
        raw = token()
        for method in (client.get, client.delete):
            response = method("/mcp", headers={"Authorization": f"Bearer {raw}"})
            assert response.status_code == 405 and response["Allow"] == "POST"

    def test_foreign_origin_is_forbidden(self, client, token):
        response = modern(client, token(), "tools/list", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_own_origin_is_allowed(self, client, token):
        response = modern(client, token(), "tools/list", headers={"Origin": settings.PUBLIC_URL})
        assert response.status_code == 200

    @pytest.mark.parametrize("body", [b"{", b"[]", b'{"jsonrpc": "2.0", "id": 1, "result": {}}'])
    def test_malformed_messages(self, client, token, body):
        response = client.post(
            "/mcp", body, content_type="application/json", headers={"Authorization": f"Bearer {token()}"}
        )
        assert response.status_code == 400 and "error" in response.json()

    def test_rate_limit(self, client, token, settings):
        from django.core.cache import cache

        cache.clear()
        settings.MCP_RATE_LIMIT_PER_MINUTE = 2
        raw = token()
        assert [modern(client, raw, "tools/list").status_code for _ in range(3)] == [200, 200, 429]
