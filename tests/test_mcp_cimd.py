from oauth2_provider.cimd import _build_application_kwargs

from skontio.mcp.cimd import reduce_grant_types

CLAUDE_DOCUMENT = {
    "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
    "client_name": "Claude",
    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
    "grant_types": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


def test_claudes_document_with_jwt_bearer_is_accepted():
    kwargs = _build_application_kwargs(reduce_grant_types(CLAUDE_DOCUMENT))
    assert kwargs["authorization_grant_type"] == "authorization-code"


def test_extra_grant_types_are_dropped_but_the_rest_is_kept():
    reduced = reduce_grant_types(CLAUDE_DOCUMENT)
    assert reduced["grant_types"] == ["authorization_code", "refresh_token"]
    assert reduced["redirect_uris"] == CLAUDE_DOCUMENT["redirect_uris"]


def test_documents_without_authorization_code_stay_rejected():
    document = {**CLAUDE_DOCUMENT, "grant_types": ["urn:ietf:params:oauth:grant-type:jwt-bearer"]}
    assert reduce_grant_types(document) == document
