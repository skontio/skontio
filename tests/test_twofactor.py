"""Zweiter Faktor für den Notfall-Login: TOTP (RFC 6238), Wiederherstellungscodes, versiegelte Geheimnisse."""

from __future__ import annotations

import time
from io import StringIO

import pytest
from django.core.management import call_command

from skontio.accounts import totp, twofactor
from skontio.accounts.models import TotpDevice
from skontio.audit.models import AuditEvent
from skontio.core.crypto import SealError, seal, unseal

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890"), RFC 6238 appendix B
LAN = {"REMOTE_ADDR": "192.168.1.20"}
PASSWORD = "ein-langes-passwort-123"


@pytest.mark.parametrize(
    ("timestamp", "code"), [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")]
)
def test_rfc6238_vectors(timestamp, code):
    assert totp.hotp(RFC_SECRET, totp.current_step(timestamp)) == code
    assert totp.verify(RFC_SECRET, code, now=timestamp) == totp.current_step(timestamp)


def test_clock_drift_window_and_replay():
    now = 1234567890
    previous = totp.hotp(RFC_SECRET, totp.current_step(now) - 1)
    assert totp.verify(RFC_SECRET, previous, now=now) is not None
    assert totp.verify(RFC_SECRET, totp.hotp(RFC_SECRET, totp.current_step(now) - 2), now=now) is None
    step = totp.verify(RFC_SECRET, "005924", now=now)
    assert totp.verify(RFC_SECRET, "005924", now=now, last_used_step=step) is None  # replay
    assert totp.verify(RFC_SECRET, "abc", now=now) is None


def test_provisioning_uri():
    uri = totp.provisioning_uri("ABC", account="inhaber")
    assert uri == "otpauth://totp/Skontio%3Ainhaber?secret=ABC&issuer=Skontio&digits=6&period=30"


def test_seal_roundtrip_and_key_binding(settings):
    token = seal("geheim", purpose="totp")
    assert "geheim" not in token and unseal(token, purpose="totp") == "geheim"
    with pytest.raises(SealError):
        unseal(token, purpose="smtp")
    settings.SECRET_KEY = "anderer-schluessel"
    with pytest.raises(SealError):
        unseal(token, purpose="totp")


def now_code(user) -> str:
    secret = unseal(TotpDevice.objects.get(user=user).secret_sealed, purpose="totp")
    return totp.hotp(secret, totp.current_step())


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user("notfall", password=PASSWORD, is_staff=True)


@pytest.fixture
def enrolled(user):
    twofactor.start_setup(user)
    codes = twofactor.confirm_setup(user, now_code(user))
    TotpDevice.objects.filter(user=user).update(last_used_step=totp.current_step() - 5)  # allow reuse in tests
    return codes


class TestServices:
    def test_seed_is_stored_sealed(self, user):
        secret = twofactor.start_setup(user)
        assert secret not in TotpDevice.objects.get(user=user).secret_sealed

    def test_wrong_code_does_not_activate(self, user):
        twofactor.start_setup(user)
        with pytest.raises(twofactor.TwoFactorError):
            twofactor.confirm_setup(user, "000000")
        assert not twofactor.is_enabled(user)

    def test_recovery_codes_work_once(self, user, enrolled):
        assert len(enrolled) == 10 and twofactor.remaining_recovery_codes(user) == 10
        assert twofactor.verify(user, enrolled[0].upper())
        assert not twofactor.verify(user, enrolled[0])
        assert twofactor.remaining_recovery_codes(user) == 9
        assert AuditEvent.objects.filter(action="auth.recovery_code_used").exists()

    def test_totp_code_cannot_be_replayed(self, user, enrolled):
        code = now_code(user)
        assert twofactor.verify(user, code)
        assert not twofactor.verify(user, code)


class TestLoginFlow:
    def login(self, client):
        return client.post("/auth/local/", {"username": "notfall", "password": PASSWORD}, **LAN)

    def test_without_second_factor_local_login_is_refused(self, client, user):
        response = self.login(client)
        assert response.status_code == 200 and "zweiter Faktor Pflicht" in response.content.decode()
        assert "_auth_user_id" not in client.session

    def test_password_then_code(self, client, user, enrolled):
        response = self.login(client)
        assert response["Location"] == "/auth/local/2fa/"
        assert "_auth_user_id" not in client.session  # not logged in after the password alone
        assert client.post("/auth/local/2fa/", {"code": "000000"}, **LAN).status_code == 200
        response = client.post("/auth/local/2fa/", {"code": now_code(user)}, **LAN)
        assert response.status_code == 302 and client.session["_auth_user_id"] == str(user.pk)
        assert AuditEvent.objects.filter(action="auth.local_login", data__second_factor=True).exists()

    def test_too_many_wrong_codes_restart_the_login(self, client, user, enrolled):
        self.login(client)
        for _ in range(5):
            response = client.post("/auth/local/2fa/", {"code": "000000"}, **LAN)
        assert response["Location"] == "/auth/local/"
        assert client.post("/auth/local/2fa/", {"code": now_code(user)}, **LAN)["Location"] == "/auth/local/"

    def test_pending_login_expires(self, client, user, enrolled, monkeypatch):
        self.login(client)
        later = time.time() + 301
        monkeypatch.setattr("skontio.accounts.views.time.time", lambda: later)
        assert client.post("/auth/local/2fa/", {"code": now_code(user)}, **LAN)["Location"] == "/auth/local/"

    def test_second_step_also_only_from_lan(self, client, user, enrolled):
        self.login(client)
        assert client.post("/auth/local/2fa/", {"code": now_code(user)}, REMOTE_ADDR="203.0.113.7").status_code == 403


class TestSecurityPage:
    def test_enrol_via_ui(self, client, user):
        client.force_login(user)
        client.post("/einstellungen/sicherheit/einrichten/")
        page = client.get("/einstellungen/sicherheit/").content.decode()
        assert "data:image/svg+xml;base64," in page and "Code aus der App" in page
        client.post("/einstellungen/sicherheit/bestaetigen/", {"code": now_code(user)})
        page = client.get("/einstellungen/sicherheit/").content.decode()
        assert "Wiederherstellungscodes" in page and twofactor.is_enabled(user)
        assert "Jetzt im Passwort-Manager" not in client.get("/einstellungen/sicherheit/").content.decode()

    def test_disable_needs_a_valid_code(self, client, user, enrolled):
        client.force_login(user)
        client.post("/einstellungen/sicherheit/aus/", {"code": "000000"})
        assert twofactor.is_enabled(user)
        client.post("/einstellungen/sicherheit/aus/", {"code": enrolled[1]})
        assert not twofactor.is_enabled(user)


def test_cli_setup_and_status(user):
    secret = twofactor.start_setup(user)
    out = StringIO()
    call_command("totp", "setup", "notfall", "--code", totp.hotp(secret, totp.current_step()), stdout=out)
    assert "2FA aktiv" in out.getvalue() and twofactor.is_enabled(user)
    out = StringIO()
    call_command("totp", "status", "notfall", stdout=out)
    assert "aktiv" in out.getvalue()
