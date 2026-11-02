"""Lieferschein: aus Angebot, Auftrag oder Rechnung, eigene Nummer, PDF ohne Preise, keine E-Rechnung."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.documents.models import Document, Rendition
from skontio.documents.renditions import render_document_html
from skontio.documents.services import (
    DocumentError,
    add_line,
    cancel_invoice,
    create_delivery_note,
    create_draft,
    finalize,
)
from skontio.payments.services import PaymentError, record_payment

DAY = dt.date(2026, 3, 2)


def finalized(shop, doc_type=Document.Type.INVOICE):
    draft = create_draft(shop.company, doc_type, shop.org, issue_date=DAY, service_date=DAY)
    add_line(draft, text="Router XR-5", description="mit Netzteil", quantity=Decimal("3"), unit_code="C62",
             unit_price=Decimal("1234.56"))  # fmt: skip
    return finalize(draft)


@pytest.mark.parametrize("doc_type", [Document.Type.INVOICE, Document.Type.QUOTE, Document.Type.ORDER_CONFIRMATION])
def test_from_every_basis_with_own_number_and_no_prices(shop, doc_type):
    basis = finalized(shop, doc_type)
    note = finalize(create_delivery_note(basis, issue_date=DAY))
    assert note.number.startswith("LS-2026-") and note.parent == basis
    html = render_document_html(note)
    assert "Lieferschein" in html and "Router XR-5" in html and "mit Netzteil" in html
    assert "1.234,56" not in html and "€" not in html and "GiroCode" not in html
    assert basis.number in html  # reference to the basis
    kinds = set(note.renditions.values_list("kind", flat=True))
    assert kinds == {Rendition.Kind.PDF}  # no e-invoice for a delivery note


def test_own_number_range_does_not_touch_invoice_numbers(shop):
    first = finalized(shop)
    finalize(create_delivery_note(first, issue_date=DAY))
    second = finalized(shop)
    assert int(second.number.rsplit("-", 1)[1]) == int(first.number.rsplit("-", 1)[1]) + 1


def test_drafts_cancellations_and_delivery_notes_are_no_basis(shop):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=DAY, service_date=DAY)
    with pytest.raises(DocumentError):
        create_delivery_note(draft, issue_date=DAY)
    storno = cancel_invoice(finalized(shop), reason="falsch", issue_date=DAY)
    with pytest.raises(DocumentError):
        create_delivery_note(storno, issue_date=DAY)
    note = finalize(create_delivery_note(finalized(shop), issue_date=DAY))
    with pytest.raises(DocumentError):
        create_delivery_note(note, issue_date=DAY)


def test_no_payment_on_a_delivery_note(shop):
    note = finalize(create_delivery_note(finalized(shop), issue_date=DAY))
    with pytest.raises(PaymentError):
        record_payment(note, Decimal("1"), DAY)


def test_english_title(shop):
    shop.org.language = "en"
    shop.org.save()
    note = finalize(create_delivery_note(finalized(shop), issue_date=DAY))
    assert "Delivery Note" in render_document_html(note)


def test_ui_button_list_filter_and_foreign_ids(client, shop):
    invoice = finalized(shop)
    client.force_login(shop.user)
    assert f"/dokumente/{invoice.pk}/lieferschein/" in client.get(f"/dokumente/{invoice.pk}/").content.decode()
    response = client.post(f"/dokumente/{invoice.pk}/lieferschein/")
    note = Document.objects.get(doc_type=Document.Type.DELIVERY_NOTE)
    assert response["Location"] == f"/dokumente/{note.pk}/"
    page = client.get("/dokumente/", {"typ": "delivery_note"}).content.decode()
    assert f"/dokumente/{note.pk}/" in page and f"/dokumente/{invoice.pk}/" not in page
    assert client.post("/dokumente/999999/lieferschein/").status_code == 404
    assert client.get(f"/dokumente/{invoice.pk}/lieferschein/").status_code == 405
