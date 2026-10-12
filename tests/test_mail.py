"""Versand festgeschriebener Dokumente per SMTP mit Versandprotokoll."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core import mail

from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.mail.models import MailLog
from skontio.mail.services import MailError, default_message, send_document
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime


@pytest.fixture(autouse=True)
def mail_settings(settings):
    settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    settings.DEFAULT_FROM_EMAIL = "rechnung@example.org"


@pytest.fixture
def company(db):
    company = Company.objects.create(
        name="Muster Selbstständig",
        street="Weg 1",
        postcode="1",
        city="S",
        tax_number="1/2/3",
        iban="DE02120300000000202051",
        email="rechnung@example.org",
        phone="+49 1",
        contact_name="Max",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def invoice(company):
    customer = create_customer(
        company,
        name="Kunde GmbH",
        street="W",
        postcode="2",
        city="K",
        email="einkauf@kunde.example",
        buyer_reference="PO-1",
    )
    doc = create_draft(
        company, Document.Type.INVOICE, customer, issue_date=dt.date(2026, 10, 11), service_date=dt.date(2026, 10, 9)
    )
    item = create_item(company, name="Beratung", unit="HUR", net_price=Decimal("80"))
    add_line_from_item(doc, item, quantity=Decimal("3"))
    return finalize(doc)


class FailingBackend:
    def __init__(self, *args, **kwargs):
        pass

    def send_messages(self, messages):
        raise OSError("Verbindung zum SMTP-Server abgelehnt")


@pytest.mark.django_db
class TestSendDocument:
    def test_sends_pdf_and_xrechnung_and_logs(self, invoice, django_user_model):
        user = django_user_model.objects.create_user("inhaber")
        subject, body = default_message(invoice)
        log = send_document(invoice, to=["einkauf@kunde.example"], subject=subject, body=body, sent_by=user)
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == ["einkauf@kunde.example"]
        assert "RE-2026-0001" in message.subject
        names = [attachment[0] for attachment in message.attachments]
        assert names == ["RE-2026-0001.pdf", "RE-2026-0001-xrechnung.xml"]
        assert log.status == MailLog.Status.SENT
        assert log.message_id and log.message_id == message.extra_headers["Message-ID"]
        assert [a["name"] for a in log.attachments] == names
        assert all(len(a["sha256"]) == 64 for a in log.attachments)

    def test_default_message_mentions_amount_and_due_date(self, invoice):
        subject, body = default_message(invoice)
        assert subject == "Rechnung RE-2026-0001 von Muster Selbstständig"
        assert "240,00 €" in body and "25.10.2026" in body

    def test_drafts_cannot_be_sent(self, company):
        customer = create_customer(company, name="K", street="W", postcode="2", city="K")
        draft = create_draft(company, Document.Type.INVOICE, customer, issue_date=dt.date(2026, 10, 11))
        with pytest.raises(MailError):
            send_document(draft, to=["a@example.org"], subject="x", body="y")

    def test_invalid_recipient_is_rejected(self, invoice):
        with pytest.raises(MailError):
            send_document(invoice, to=["kein-mail"], subject="x", body="y")

    def test_header_injection_is_rejected(self, invoice):
        with pytest.raises(MailError):
            send_document(invoice, to=["a@example.org"], subject="Hallo\nBcc: opfer@example.org", body="y")

    def test_missing_smtp_server_is_reported(self, invoice, settings):
        settings.MAILERS = {"default": {"BACKEND": settings.SMTP_BACKEND, "OPTIONS": {"host": ""}}}
        with pytest.raises(MailError, match="SMTP"):
            send_document(invoice, to=["a@example.org"], subject="x", body="y")
        assert not MailLog.objects.exists()

    def test_failure_is_logged(self, invoice, settings):
        settings.MAILERS = {"default": {"BACKEND": "test_mail.FailingBackend"}}
        with pytest.raises(MailError):
            send_document(invoice, to=["a@example.org"], subject="x", body="y")
        log = MailLog.objects.get()
        assert log.status == MailLog.Status.FAILED
        assert "Verbindung" in log.error


@pytest.mark.django_db
def test_send_from_ui(client, invoice, django_user_model):
    client.force_login(django_user_model.objects.create_user("inhaber", is_staff=True))
    body = client.get(f"/dokumente/{invoice.pk}/").content.decode()
    assert "einkauf@kunde.example" in body  # recipient prefilled
    response = client.post(
        f"/dokumente/{invoice.pk}/senden/",
        {"to": "einkauf@kunde.example", "cc": "", "subject": "Rechnung", "body": "Hallo"},
    )
    assert response.status_code == 302
    assert len(mail.outbox) == 1
    assert "Versendet" in client.get(f"/dokumente/{invoice.pk}/").content.decode()


class TestMailersFromEnvironment:
    def test_starttls_default(self, monkeypatch):
        from skontio.settings import SMTP_BACKEND, mailers_from_env

        monkeypatch.setenv("SKONTIO_SMTP_HOST", "smtp.example.org")
        monkeypatch.setenv("SKONTIO_SMTP_PASSWORD", "geheim")
        monkeypatch.delenv("SKONTIO_SMTP_SECURITY", raising=False)
        monkeypatch.delenv("SKONTIO_SMTP_PORT", raising=False)
        config = mailers_from_env()["default"]
        assert config["BACKEND"] == SMTP_BACKEND
        options = config["OPTIONS"]
        assert (options["host"], options["port"], options["use_tls"], options["use_ssl"]) == (
            "smtp.example.org",
            587,
            True,
            False,
        )
        assert options["password"] == "geheim"

    def test_implicit_tls_uses_port_465(self, monkeypatch):
        from skontio.settings import mailers_from_env

        monkeypatch.setenv("SKONTIO_SMTP_SECURITY", "ssl")
        monkeypatch.delenv("SKONTIO_SMTP_PORT", raising=False)
        options = mailers_from_env()["default"]["OPTIONS"]
        assert (options["port"], options["use_tls"], options["use_ssl"]) == (465, False, True)

    def test_password_from_secret_file(self, monkeypatch, tmp_path):
        from skontio.settings import mailers_from_env

        secret = tmp_path / "smtp_password"
        secret.write_text("aus-datei\n")
        monkeypatch.setenv("SKONTIO_SMTP_PASSWORD_FILE", str(secret))
        assert mailers_from_env()["default"]["OPTIONS"]["password"] == "aus-datei"

    def test_unknown_security_is_rejected(self, monkeypatch):
        from skontio.settings import mailers_from_env

        monkeypatch.setenv("SKONTIO_SMTP_SECURITY", "tls")
        with pytest.raises(RuntimeError):
            mailers_from_env()

    def test_console_backend_takes_no_smtp_options(self, monkeypatch):
        from skontio.settings import mailers_from_env

        monkeypatch.setenv("SKONTIO_MAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
        assert mailers_from_env() == {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}
