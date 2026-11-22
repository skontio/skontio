"""Admins hear about unattended failures: once per streak, never twice for the same invoice."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone

from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.mail.models import MailLog
from skontio.ops import scheduler
from skontio.recurring.models import RecurringRun
from skontio.recurring.services import create_from_invoice, generate_due, retry_failed


@pytest.fixture(autouse=True)
def _mail(settings):
    settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    settings.DEFAULT_FROM_EMAIL = "rechnung@example.org"


@pytest.fixture
def admin(db):
    return get_user_model().objects.create_user("chef", email="chef@example.org", password="x", is_staff=True)


def boom():
    raise OSError("Server nicht erreichbar")


def test_failing_interval_job_mails_once_per_streak(admin):
    start = timezone.now()
    scheduler._interval("bank_fetch", dt.timedelta(hours=1), boom, start, label="Kontoabruf")
    scheduler._interval("bank_fetch", dt.timedelta(hours=1), boom, start + dt.timedelta(hours=2), label="Kontoabruf")
    assert [m.to for m in mail.outbox] == [["chef@example.org"]]
    assert "Kontoabruf" in mail.outbox[0].subject
    assert "Server nicht erreichbar" in mail.outbox[0].body


def test_a_new_streak_after_success_mails_again(admin):
    start = timezone.now()
    scheduler._interval("bank_fetch", dt.timedelta(hours=1), boom, start, label="Kontoabruf")
    scheduler._interval("bank_fetch", dt.timedelta(hours=1), lambda: "ok", start + dt.timedelta(hours=2))
    scheduler._interval("bank_fetch", dt.timedelta(hours=1), boom, start + dt.timedelta(hours=4), label="Kontoabruf")
    assert len(mail.outbox) == 2


def test_no_admin_address_does_not_break_the_job(db):
    row = scheduler._interval("bank_fetch", dt.timedelta(hours=1), boom, timezone.now(), label="Kontoabruf")
    assert row.status == "failed"
    assert mail.outbox == []


class TestAutoSendFailure:
    def invoice_run(self, shop, today):
        doc = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=today, service_date=today)
        add_line_from_item(doc, shop.item, quantity=Decimal("1"))
        recurring = create_from_invoice(finalize(doc), name="Hosting", interval=1, start_date=today, auto_send=True)
        return recurring

    def test_admin_is_told_and_the_invoice_is_retried_without_double_send(self, shop, admin):
        today = timezone.localdate()
        self.invoice_run(shop, today)
        shop.org.email = ""
        shop.org.save()
        (draft,) = generate_due(shop.company, today=today, auto_send=True)
        run = RecurringRun.objects.get(document=draft)
        assert run.send_failed_at is not None
        assert [m.to for m in mail.outbox] == [["chef@example.org"]]
        assert "Rechnungsversand fehlgeschlagen" in mail.outbox[0].subject

        shop.org.email = "a@kunde.example"
        shop.org.save()
        mail.outbox.clear()
        assert retry_failed() == 1
        run.refresh_from_db()
        draft.refresh_from_db()
        assert run.send_failed_at is None
        assert draft.status == Document.Status.FINALIZED
        assert [m.to for m in mail.outbox] == [["a@kunde.example"]]

        assert retry_failed() == 0  # nothing left to retry
        assert MailLog.objects.filter(document=draft, status="sent").count() == 1

    def test_failure_mail_names_customer_and_link(self, shop, admin):
        today = timezone.localdate()
        self.invoice_run(shop, today)
        shop.org.email = ""
        shop.org.save()
        generate_due(shop.company, today=today, auto_send=True)
        (alert,) = [m for m in mail.outbox if "Rechnungsversand" in m.subject]
        assert shop.org.name in alert.subject
        assert "/dokumente/" in alert.body

    def test_retry_gives_up_after_a_week(self, shop, admin):
        today = timezone.localdate()
        self.invoice_run(shop, today)
        shop.org.email = ""
        shop.org.save()
        generate_due(shop.company, today=today, auto_send=True)
        RecurringRun.objects.update(send_failed_at=timezone.now() - dt.timedelta(days=8))
        shop.org.email = "a@kunde.example"
        shop.org.save()
        assert retry_failed() == 0
