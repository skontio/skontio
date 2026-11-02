"""Standardanhänge (AGB, Datenschutz): einmal hochladen, beim Versand passend vorausgewählt, nur PDF."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile

from skontio.attachments.models import Attachment
from skontio.attachments.services import AttachmentError, add_standard_file, standard_files
from skontio.core.models import Company
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.mail.services import MailError, send_document

DAY = dt.date(2026, 3, 2)
AGB = b"%PDF-1.4 Allgemeine Geschaeftsbedingungen"
PRIVACY = b"%PDF-1.4 Datenschutzerklaerung"


@pytest.fixture(autouse=True)
def mail_settings(settings):
    settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    settings.DEFAULT_FROM_EMAIL = "rechnung@example.org"


def finalized(shop, doc_type=Document.Type.QUOTE):
    draft = create_draft(shop.company, doc_type, shop.org, issue_date=DAY, service_date=DAY)
    add_line_from_item(draft, shop.item, quantity=Decimal("1"))
    return finalize(draft)


def ticked(page: str) -> set[int]:
    return {int(v) for v in re.findall(r'<input type="checkbox" name="extra" value="(\d+)"[^>]*\bchecked', page)}


def test_files_by_document_type(shop):
    agb = add_standard_file(shop.company, AGB, filename="AGB.pdf", send_with=["quote", "invoice"])
    privacy = add_standard_file(shop.company, PRIVACY, filename="Datenschutz.pdf", send_with=["quote"])
    assert set(standard_files(shop.company, "quote")) == {agb, privacy}
    assert standard_files(shop.company, "partial_invoice") == [agb]
    assert standard_files(shop.company, "order_confirmation") == []


def test_only_pdf_and_a_chosen_type(shop):
    with pytest.raises(AttachmentError, match="PDF"):
        add_standard_file(shop.company, b"\x89PNG\r\n\x1a\nbild", filename="agb.png", send_with=["quote"])
    with pytest.raises(AttachmentError, match="Dokumenten"):
        add_standard_file(shop.company, AGB, filename="agb.pdf", send_with=[])
    with pytest.raises(AttachmentError, match="Dokumenten"):
        add_standard_file(shop.company, AGB, filename="agb.pdf", send_with=["mahnung"])


def test_sent_with_a_quote(shop):
    agb = add_standard_file(shop.company, AGB, filename="AGB.pdf", send_with=["quote"])
    quote = finalized(shop)
    send_document(quote, to=["a@kunde.example"], subject="Angebot", body="Hallo", extra=[agb])
    names = [name for name, _, _ in mail.outbox[0].attachments]
    assert names[0].startswith(quote.number) and "AGB.pdf" in names


def test_another_companys_file_is_refused(shop):
    other = Company.objects.create(name="Andere", street="W", postcode="1", city="S", tax_number="9/9/9")
    foreign = add_standard_file(other, AGB, filename="AGB.pdf", send_with=["quote"])
    with pytest.raises(MailError, match="gehört nicht"):
        send_document(finalized(shop), to=["a@kunde.example"], subject="A", body="B", extra=[foreign])


def test_send_form_preselects_by_type_and_unticked_files_stay_home(client, shop):
    agb = add_standard_file(shop.company, AGB, filename="AGB.pdf", send_with=["quote"])
    privacy = add_standard_file(shop.company, PRIVACY, filename="Datenschutz.pdf", send_with=["invoice"])
    quote = finalized(shop)
    client.force_login(shop.user)
    page = client.get(f"/dokumente/{quote.pk}/").content.decode()
    assert ticked(page) == {agb.pk} and "Datenschutz.pdf" in page  # offered, but only AGB ticked for quotes
    client.post(f"/dokumente/{quote.pk}/senden/", {"to": "a@kunde.example", "subject": "Angebot", "body": "Hallo"})
    assert len(mail.outbox[0].attachments) == 1  # nothing ticked in the post: only the quote itself
    invoice = finalized(shop, Document.Type.INVOICE)
    assert ticked(client.get(f"/dokumente/{invoice.pk}/").content.decode()) == {privacy.pk}


def test_upload_and_remove_in_settings(client, shop):
    client.force_login(shop.user)
    upload = SimpleUploadedFile("AGB 2026.pdf", AGB, content_type="application/pdf")
    response = client.post("/einstellungen/vorlagen/anhaenge/", {"file": upload, "send_with": ["quote", "kaputt"]})
    assert response.status_code == 302
    agb = Attachment.objects.get()
    assert agb.send_with == ["quote"] and agb.customer is None and agb.document is None
    assert "AGB 2026.pdf" in client.get("/einstellungen/vorlagen/").content.decode()
    response = client.post(f"/dateien/{agb.pk}/loeschen/")
    assert response["Location"] == "/einstellungen/vorlagen/#anhaenge" and not Attachment.objects.exists()
