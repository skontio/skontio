"""Client ID Metadata Documents: tolerate extra grant types that Skontio never uses.

Claude's document lists `urn:ietf:params:oauth:grant-type:jwt-bearer` next to `authorization_code`. django-oauth-toolkit
rejects a document with more than one non-refresh grant ("Invalid client_id"), so the connector could not be
re-authorised. Skontio only ever offers the authorization-code flow with PKCE; any other grant type in a client's
document is dropped here, and the token endpoint still refuses it.
"""

from oauth2_provider.cimd import SafeMetadataFetcher

SUPPORTED_GRANT_TYPES = {"authorization_code", "refresh_token"}


def reduce_grant_types(metadata: dict) -> dict:
    grants = metadata.get("grant_types")
    if isinstance(grants, list) and "authorization_code" in grants and all(isinstance(g, str) for g in grants):
        metadata = {**metadata, "grant_types": [g for g in grants if g in SUPPORTED_GRANT_TYPES]}
    return metadata


class SkontioMetadataFetcher(SafeMetadataFetcher):
    def fetch(self, client_id):
        metadata, max_age = super().fetch(client_id)
        return reduce_grant_types(metadata), max_age
