"""``skontio doctor oidc``: diagnose the Skontio → identity provider connection."""

from __future__ import annotations

import datetime as dt
import smtplib
import ssl
from email.utils import format_datetime

import httpx
import pytest

from skontio.core.doctor import check_backup, check_oidc, check_smtp

ISSUER = "https://id.example.org"
NOW = dt.datetime(2026, 10, 11, 18, 0, tzinfo=dt.UTC)

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/api/oidc/token",
    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
    "code_challenge_methods_supported": ["plain", "S256"],
    "scopes_supported": ["openid", "profile", "email", "groups"],
    "claims_supported": ["sub", "email", "groups"],
    "end_session_endpoint": f"{ISSUER}/api/oidc/end-session",
}


def client_for(discovery: dict | None = None, *, jwks: dict | None = None, date: dt.datetime = NOW, fail=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise fail
        headers = {"Date": format_datetime(date, usegmt=True)}
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=discovery or DISCOVERY, headers=headers)
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json=jwks if jwks is not None else {"keys": [{"kid": "1"}]}, headers=headers)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def by_name(checks):
    return {check.name: check for check in checks}


def test_healthy_provider_passes_all_checks():
    checks = check_oidc(ISSUER, http_client=client_for(), now=NOW)
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]


def test_unreachable_provider_suggests_split_dns():
    checks = by_name(check_oidc(ISSUER, http_client=client_for(fail=httpx.ConnectError("refused")), now=NOW))
    assert not checks["discovery"].ok
    assert "Split-DNS" in checks["discovery"].hint


def test_issuer_mismatch_is_reported():
    checks = by_name(check_oidc(ISSUER, http_client=client_for({**DISCOVERY, "issuer": ISSUER + "/"}), now=NOW))
    assert not checks["issuer"].ok


def test_missing_s256_is_reported():
    discovery = {**DISCOVERY, "code_challenge_methods_supported": ["plain"]}
    assert not by_name(check_oidc(ISSUER, http_client=client_for(discovery), now=NOW))["pkce"].ok


def test_empty_jwks_is_reported():
    assert not by_name(check_oidc(ISSUER, http_client=client_for(jwks={"keys": []}), now=NOW))["jwks"].ok


def test_missing_groups_claim_is_reported():
    discovery = {**DISCOVERY, "scopes_supported": ["openid"], "claims_supported": ["sub"]}
    assert not by_name(check_oidc(ISSUER, http_client=client_for(discovery), now=NOW))["groups"].ok


@pytest.mark.parametrize(("skew", "ok"), [(30, True), (-30, True), (90, False), (-90, False)])
def test_clock_skew(skew, ok):
    client = client_for(date=NOW + dt.timedelta(seconds=skew))
    assert by_name(check_oidc(ISSUER, http_client=client, now=NOW))["clock"].ok is ok


class FakeMailer:
    def __init__(self, error=None):
        self.error, self.closed = error, False

    def open(self):
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


def test_smtp_login_ok_closes_connection():
    mailer = FakeMailer()
    assert [c.ok for c in check_smtp(mailer, host="smtp.example.org")] == [True]
    assert mailer.closed


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad"), "login"),
        (ssl.SSLError("wrong version number"), "tls"),
        (ConnectionRefusedError("refused"), "server"),
    ],
)
def test_smtp_failures_name_the_cause(error, name):
    [check] = check_smtp(FakeMailer(error), host="smtp.example.org")
    assert (check.ok, check.name) == (False, name)
    assert check.hint


def test_smtp_without_host():
    [check] = check_smtp(FakeMailer(), host="")
    assert not check.ok


def test_backup_dir_must_be_writable(tmp_path):
    assert [c.ok for c in check_backup(tmp_path, vendor="sqlite")] == [True]
    assert [c.ok for c in check_backup(tmp_path / "fehlt", vendor="sqlite")] == [False]


def test_backup_reports_old_pg_dump(tmp_path):
    def too_old():
        raise RuntimeError("pg_dump 17 ist älter als der Postgres-Server 18")

    checks = check_backup(tmp_path, vendor="postgresql", pg_versions=too_old)
    assert [c.ok for c in checks] == [True, False]
    assert "älter" in checks[1].detail
    assert check_backup(tmp_path, vendor="postgresql", pg_versions=lambda: (18, 17))[1].ok
