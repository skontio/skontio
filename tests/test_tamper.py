"""Angriffe auf gespeicherte Daten: Protokoll umschreiben, IBAN tauschen, Rechte der Datenbankrolle.

Der Angreifer hat hier Schreibzugriff auf die Datenbank (gestohlene Zugangsdaten, SQL-Konsole, manipuliertes
Backup) – aber nicht den SKONTIO_SECRET_KEY. Jede Manipulation muss auffallen.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac

import pytest
from django.core import mail
from django.db import connection

from skontio.audit import services as audit
from skontio.audit.models import AuditEvent, AuditHead
from skontio.core.doctor import check_db_role
from skontio.core.watch import report_changes, snapshot

GENESIS = audit.GENESIS


def chain(n: int) -> None:
    for i in range(n):
        audit.record("test.event", data={"i": i})


def rewrite_from(seq: int, *, hasher, change: dict) -> None:
    """What an attacker with only database access does: delete from ``seq`` on and write a new history."""
    events = list(AuditEvent.objects.filter(seq__gte=seq).order_by("seq"))
    AuditEvent.objects.filter(seq__gte=seq).delete()
    prev = AuditEvent.objects.filter(seq=seq - 1).values_list("hash", flat=True).first() or GENESIS
    for event in events:
        event.pk = None
        if event.seq == seq:
            event.data = {**event.data, **change}
        event.prev_hash = prev
        event.hash = hasher(prev, audit._hashed_fields(event))
        event.save()
        prev = event.hash
    AuditHead.objects.filter(pk=1).update(hash=prev)


def sha256(prev, fields):
    return audit._legacy_hash(prev, fields)


def wrong_key(prev, fields):
    return hmac.new(b"geraten", audit._payload(prev, fields), hashlib.sha256).hexdigest()


@pytest.mark.django_db
class TestAuditChain:
    def test_untouched_chain_is_fine(self):
        chain(5)
        assert audit.verify_chain() == []

    @pytest.mark.parametrize("hasher", [sha256, wrong_key])
    def test_recomputed_history_is_detected_without_the_key(self, hasher):
        chain(6)
        rewrite_from(3, hasher=hasher, change={"i": "manipuliert"})
        assert any("verändert" in p for p in audit.verify_chain())

    def test_deleting_the_newest_events_is_detected(self):
        chain(4)
        AuditEvent.objects.filter(seq__gte=3).delete()
        assert any("Ende der Kette" in p for p in audit.verify_chain())

    def test_updates_are_refused_by_the_database(self):
        chain(1)
        with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
            cursor.execute("UPDATE audit_auditevent SET action = 'x'")

    def test_old_plain_chains_are_pinned_by_the_keyed_marker(self):
        AuditEvent.objects.all().delete()
        AuditHead.objects.all().delete()
        prev = GENESIS
        for seq in (1, 2, 3):  # an installation from before the key: plain SHA-256
            event = AuditEvent(seq=seq, created_at=dt.datetime(2026, 1, seq, tzinfo=dt.UTC), actor_kind="system",
                               action="alt", data={"n": seq}, prev_hash=prev)  # fmt: skip
            event.hash = audit._legacy_hash(prev, audit._hashed_fields(event))
            event.save()
            prev = event.hash
        AuditHead.objects.create(pk=1, seq=3, hash=prev)
        marker = audit.key_chain()
        audit.record("neu")
        assert marker.action == audit.KEYED_MARKER and audit.verify_chain() == []
        assert audit.key_chain() is None  # only once
        rewrite_from(2, hasher=sha256, change={"n": "manipuliert"})  # old part rewritten, marker recomputed plainly
        assert audit.verify_chain() != []

    def test_marker_removed_old_events_fail(self):
        chain(2)
        AuditEvent.objects.filter(action=audit.KEYED_MARKER).delete()
        rewrite_from(1, hasher=sha256, change={})
        assert audit.verify_chain() != []

    def test_backup_anchor_reveals_a_rewrite_even_with_the_key(self):
        chain(5)
        anchor = audit.head()
        rewrite_from(2, hasher=audit._event_hash, change={"i": "mit Schlüssel"})  # attacker inside the app
        assert audit.verify_chain() == []  # the chain alone cannot tell
        assert audit.verify_anchor(anchor) != []
        assert audit.verify_anchor({"seq": 0}) == []


@pytest.mark.django_db
def test_backup_manifest_carries_the_anchor(tmp_path):
    from skontio.ops.backup import create_backup, verify_backup

    chain(2)
    manifest = verify_backup(create_backup(tmp_path))
    assert manifest["audit_head"] == audit.head()


@pytest.mark.django_db
class TestIbanFraud:
    @pytest.fixture(autouse=True)
    def mailbox(self, settings):
        settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}

    def test_changing_the_iban_warns_every_admin(self, client, shop, django_user_model):
        django_user_model.objects.create_user("zweite", is_staff=True, email="zweite@example.org")
        client.force_login(shop.user)
        before = snapshot(shop.company)
        fields = (
            "name", "street", "postcode", "city", "country", "activity", "tax_number", "vat_id", "email", "phone",
            "contact_name", "bic", "account_holder", "vat_period", "vat_basis", "payment_link", "legal_form",
        )  # fmt: skip
        data = {f: getattr(shop.company, f) or "" for f in fields}
        data |= {"iban": "DE89370400440532013000", "default_payment_terms_days": "14"}
        response = client.post("/einrichtung/", data)
        assert response.status_code == 302, response.content.decode()[:2000]
        event = AuditEvent.objects.get(action="company.sensitive_change")
        assert event.data["iban"] == {"from": f"… {before['iban'][-4:]}", "to": "… 3000"}
        assert len(mail.outbox) == 1 and "zweite@example.org" in mail.outbox[0].to
        assert "… 3000" in mail.outbox[0].body and "DE89370400440532013000" not in mail.outbox[0].body

    def test_no_change_no_alarm(self, shop):
        assert report_changes(shop.company, snapshot(shop.company), user=shop.user) == []
        assert mail.outbox == []

    def test_wizard_bank_step_is_watched_too(self, client, shop):
        client.force_login(shop.user)
        data = {"iban": "DE89370400440532013000", "bic": "", "account_holder": "X"}
        client.post("/einrichtung/assistent/bank/", data)
        assert AuditEvent.objects.filter(action="company.sensitive_change").exists() and len(mail.outbox) == 1


def test_sessions_expire_after_a_working_day(settings):
    assert settings.SESSION_COOKIE_AGE == 12 * 3600


@pytest.mark.django_db
def test_doctor_db_role():
    checks = check_db_role(connection)
    if connection.vendor == "postgresql":
        assert {c.name for c in checks} == {"superuser", "rechte"}  # the CI's test role may well be a superuser
    else:
        assert all(check.ok for check in checks)
