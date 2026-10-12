"""Mahnwesen: fällige Stufen, Mahnschreiben (PDF, Nummer, Archiv), Versand und Oberfläche."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.core import mail
from pypdf import PdfReader

from skontio.archive.services import load
from skontio.audit.models import AuditEvent
from skontio.core.models import Company
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, cancel_invoice, create_draft, finalize
from skontio.dunning.models import DunningLevel
from skontio.dunning.services import (
    DunningError,
    candidates,
    create_notice,
    default_mail,
    levels,
    overdue,
    render_notice_html,
    send_notice,
)
from skontio.payments.services import record_payment

D = Decimal
ISSUED = dt.date(2026, 9, 1)


@pytest.fixture
def smtp(settings):
    settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}


def invoice(shop, issued=ISSUED):
    doc = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=issued, service_date=issued)
    add_line_from_item(doc, shop.item, quantity=D("1"))
    return finalize(doc)


def pdf_text(blob) -> str:
    return "".join(page.extract_text() for page in PdfReader(io.BytesIO(load(blob))).pages)


class TestState:
    def test_levels_are_created_with_defaults(self, shop):
        names = [level.name for level in levels(shop.company)]
        assert names == ["Zahlungserinnerung", "1. Mahnung", "2. Mahnung"]

    def test_first_level_is_due_seven_days_after_due_date(self, shop):
        doc = invoice(shop)
        assert overdue(shop.company, doc.due_date) == []  # due today: not overdue
        waiting = overdue(shop.company, doc.due_date + dt.timedelta(days=3))
        assert [c.invoice for c in waiting] == [doc] and waiting[0].next_level.level == 1
        assert candidates(shop.company, doc.due_date + dt.timedelta(days=3)) == []
        due = candidates(shop.company, doc.due_date + dt.timedelta(days=7))
        assert [c.invoice for c in due] == [doc]

    def test_next_level_waits_after_the_last_letter(self, shop):
        doc = invoice(shop)
        day = doc.due_date + dt.timedelta(days=8)
        create_notice(doc, issued_on=day)
        assert candidates(shop.company, day + dt.timedelta(days=13)) == []
        (due,) = candidates(shop.company, day + dt.timedelta(days=14))
        assert due.next_level.name == "1. Mahnung"

    def test_paid_and_cancelled_invoices_are_never_dunned(self, shop):
        paid = invoice(shop)
        record_payment(paid, paid.gross_total, paid.due_date)
        cancelled = invoice(shop)
        cancel_invoice(cancelled, issue_date=ISSUED, reason="Fehler")
        later = ISSUED + dt.timedelta(days=60)
        assert overdue(shop.company, later) == []
        with pytest.raises(DunningError, match="storniert"):
            create_notice(cancelled, issued_on=later)
        with pytest.raises(DunningError, match="bezahlt"):
            create_notice(paid, issued_on=later)

    def test_partial_payment_reduces_the_open_amount(self, shop):
        doc = invoice(shop)
        record_payment(doc, D("50"), doc.due_date)
        notice = create_notice(doc, issued_on=doc.due_date + dt.timedelta(days=10))
        assert notice.open_amount == doc.gross_total - D("50")


class TestNotice:
    def test_letter_is_numbered_archived_and_audited(self, shop):
        doc = invoice(shop)
        levels(shop.company)
        DunningLevel.objects.filter(company=shop.company, level=1).update(fee=D("5.00"))
        day = doc.due_date + dt.timedelta(days=10)
        notice = create_notice(doc, issued_on=day)
        assert notice.number == "MA-2026-0001" and notice.title == "Zahlungserinnerung"
        assert notice.pay_by == day + dt.timedelta(days=7) and notice.total == doc.gross_total + D("5.00")
        assert notice.blob.retention_until == dt.date(2032, 12, 31)
        text = pdf_text(notice.blob)
        assert "Zahlungserinnerung" in text and doc.number in text and "Mahngebühr" in text
        assert AuditEvent.objects.filter(action="dunning.create").exists()

    def test_all_levels_and_then_stop(self, shop):
        doc = invoice(shop)
        day = doc.due_date + dt.timedelta(days=60)
        titles = [create_notice(doc, issued_on=day).title for _ in range(3)]
        assert titles == ["Zahlungserinnerung", "1. Mahnung", "2. Mahnung"]
        html = render_notice_html(doc.dunning_notices.get(level=3))
        assert "Bisherige Schreiben: Zahlungserinnerung" in html and "trotz unseres letzten Schreibens" in html
        with pytest.raises(DunningError, match="ausgeschöpft"):
            create_notice(doc, issued_on=day)

    def test_english_letter(self, shop):
        shop.org.language = "en"
        shop.org.save()
        doc = invoice(shop)
        notice = create_notice(doc, issued_on=doc.due_date + dt.timedelta(days=10))
        assert notice.title == "Payment Reminder"
        html = render_notice_html(notice)
        assert "Dear Sir or Madam," in html and "Amount outstanding" in html
        subject, body = default_mail(notice)
        assert subject == f"Payment Reminder for invoice {doc.number}" and "Kind regards" in body

    def test_send_attaches_letter_and_invoice(self, shop, smtp):
        doc = invoice(shop)
        notice = create_notice(doc, issued_on=doc.due_date + dt.timedelta(days=10))
        subject, body = default_mail(notice)
        send_notice(notice, to=["buchhaltung@kunde.example"], subject=subject, body=body, user=shop.user)
        (message,) = mail.outbox
        assert [a[0] for a in message.attachments] == [f"{notice.number}.pdf", f"{doc.number}.pdf"]
        notice.refresh_from_db()
        assert notice.sent_at is not None
        assert doc.mail_logs.get().subject == subject
        assert shop.org.activities.filter(text__contains=notice.number).exists()


class TestWeb:
    def test_overview_create_and_send(self, client, shop, smtp):
        doc = invoice(shop, issued=dt.date(2026, 1, 5))
        client.force_login(shop.user)
        assert "1 Mahnung fällig" in client.get("/").content.decode()
        page = client.get("/mahnwesen/").content.decode()
        assert doc.number in page and "Erstellen" in page
        response = client.post(f"/mahnwesen/rechnung/{doc.pk}/")
        assert response["Location"] == f"/dokumente/{doc.pk}/#mahnungen"
        notice = doc.dunning_notices.get()
        invoice_page = client.get(f"/dokumente/{doc.pk}/").content.decode()
        assert notice.number in invoice_page and "per E-Mail senden" in invoice_page
        assert client.get(f"/mahnwesen/{notice.pk}/pdf/")["Content-Type"] == "application/pdf"
        client.post(
            f"/mahnwesen/{notice.pk}/senden/", {"to": "a@kunde.example", "subject": "Erinnerung", "body": "Hallo"}
        )
        assert len(mail.outbox) == 1

    def test_levels_page(self, client, shop):
        client.force_login(shop.user)
        data = {}
        for level in levels(shop.company):
            prefix = f"l{level.level}"
            data |= {
                f"{prefix}-name": level.name,
                f"{prefix}-days_after": "10",
                f"{prefix}-pay_days": "7",
                f"{prefix}-fee": "2.50" if level.level == 3 else "0",
                f"{prefix}-text": "",
            }
        assert client.post("/mahnwesen/stufen/", data).status_code == 302
        assert DunningLevel.objects.get(company=shop.company, level=3).fee == D("2.50")

    def test_other_company_is_hidden(self, client, shop):
        doc = invoice(shop)
        notice = create_notice(doc, issued_on=doc.due_date + dt.timedelta(days=10))
        other = Company.objects.create(name="Fremd")
        notice.company = other
        notice.save()
        client.force_login(shop.user)
        assert client.get(f"/mahnwesen/{notice.pk}/pdf/").status_code == 404
