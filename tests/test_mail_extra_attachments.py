"""Weitere Dateien beim Versand: nur aus der Ablage des Dokuments oder Kunden, geprüft, mit Größengrenze."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core import mail

from skontio.archive.services import _root
from skontio.attachments.services import add_attachment
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.mail import services as mail_services
from skontio.mail.models import MailLog
from skontio.mail.services import MailError, send_document

DAY = dt.date(2026, 3, 2)
PDF = b"%PDF-1.4 Leistungsnachweis"


@pytest.fixture(autouse=True)
def mail_settings(settings):
    settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    settings.DEFAULT_FROM_EMAIL = "rechnung@example.org"


def invoice(shop, customer=None):
    draft = create_draft(shop.company, Document.Type.INVOICE, customer or shop.org, issue_date=DAY, service_date=DAY)
    add_line_from_item(draft, shop.item, quantity=Decimal("1"))
    return finalize(draft)


def send(doc, extra):
    return send_document(doc, to=["a@kunde.example"], subject="Rechnung", body="Hallo", extra=extra)


def test_document_and_customer_files_are_sent_along_and_logged(shop):
    doc = invoice(shop)
    report = add_attachment(shop.company, PDF, filename="nachweis.pdf", document=doc)
    contract = add_attachment(shop.company, b"%PDF-1.4 Vertrag", filename="vertrag.pdf", customer=shop.org)
    log = send(doc, [report, contract, report])
    names = [name for name, _, _ in mail.outbox[0].attachments]
    assert names[0].startswith(doc.number) and names.count("nachweis.pdf") == 1 and "vertrag.pdf" in names
    assert {"nachweis.pdf", "vertrag.pdf"} <= {a["name"] for a in log.attachments}


def test_files_of_another_document_or_customer_are_refused(shop):
    doc = invoice(shop)
    other_doc = invoice(shop, customer=shop.person)
    foreign = add_attachment(shop.company, PDF, filename="fremd.pdf", document=other_doc)
    stranger = create_customer(shop.company, name="Dritter", street="X", postcode="1", city="Y")
    foreign_customer = add_attachment(shop.company, PDF + b"2", filename="dritter.pdf", customer=stranger)
    for wrong in (foreign, foreign_customer):
        with pytest.raises(MailError, match="gehört nicht"):
            send(doc, [wrong])
    assert mail.outbox == [] and not MailLog.objects.exists()


def test_altered_archive_file_is_never_sent(shop):
    doc = invoice(shop)
    attachment = add_attachment(shop.company, PDF, filename="nachweis.pdf", document=doc)
    path = _root() / attachment.blob.storage_key
    path.chmod(0o600)
    path.write_bytes(b"manipuliert")
    with pytest.raises(Exception, match="verändert"):
        send(doc, [attachment])
    assert mail.outbox == []


def test_too_large_in_total_is_refused(shop, monkeypatch):
    monkeypatch.setattr(mail_services, "MAX_MAIL_BYTES", 1000)
    doc = invoice(shop)
    big = add_attachment(shop.company, b"%PDF-1.4 " + b"x" * 2000, filename="gross.pdf", document=doc)
    with pytest.raises(MailError, match="20 MB"):
        send(doc, [big])
    assert mail.outbox == []


def test_send_form_offers_only_own_files_and_rejects_forged_ids(client, shop):
    doc = invoice(shop)
    own = add_attachment(shop.company, PDF, filename="nachweis.pdf", document=doc)
    other = add_attachment(shop.company, PDF + b"x", filename="fremd.pdf", document=invoice(shop, shop.person))
    client.force_login(shop.user)
    page = client.get(f"/dokumente/{doc.pk}/").content.decode()
    assert "nachweis.pdf" in page and "fremd.pdf" not in page.split('id="senden"', 1)[1]
    form = {"to": "a@kunde.example", "subject": "Rechnung", "body": "Hallo"}
    response = client.post(f"/dokumente/{doc.pk}/senden/", {**form, "extra": [other.pk]})
    assert response.status_code == 200 and mail.outbox == []  # invalid choice, form shown again
    for garbage in (["abc"], ["-1"], ["99999999999999999999"]):
        assert client.post(f"/dokumente/{doc.pk}/senden/", {**form, "extra": garbage}).status_code == 200
    assert mail.outbox == []
    assert client.post(f"/dokumente/{doc.pk}/senden/", {**form, "extra": [own.pk]}).status_code == 302
    assert "nachweis.pdf" in [name for name, _, _ in mail.outbox[0].attachments]


def test_without_files_the_form_has_no_extra_field(client, shop):
    doc = invoice(shop)
    client.force_login(shop.user)
    assert "Weitere Dateien anhängen" not in client.get(f"/dokumente/{doc.pk}/").content.decode()
