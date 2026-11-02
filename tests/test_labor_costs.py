"""Arbeitskosten nach § 35a EStG: Anteil je Position, nach Gesamtrabatt, mit USt, nur für Privatkunden."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.catalog.services import create_item
from skontio.documents.models import Document
from skontio.documents.renditions import render_document_html
from skontio.documents.services import (
    DocumentError,
    add_line,
    add_line_from_item,
    create_delivery_note,
    create_draft,
    finalize,
    labor_costs,
)

DAY = dt.date(2026, 3, 2)


def draft(shop, customer, discount="0"):
    return create_draft(shop.company, Document.Type.INVOICE, customer, issue_date=DAY, service_date=DAY,
                        document_discount_percent=Decimal(discount))  # fmt: skip


def line(doc, price, labor, text="Montage"):
    return add_line(doc, text=text, quantity=Decimal("1"), unit_code="LS", unit_price=Decimal(price),
                    labor_percent=Decimal(labor))  # fmt: skip


def test_labor_share_with_vat_on_the_private_invoice(shop):
    doc = draft(shop, shop.person)
    line(doc, "200", "100")  # pure labour
    line(doc, "300", "0", text="Material")
    invoice = finalize(doc)
    costs = labor_costs(invoice)
    assert (costs.net, costs.vat, costs.gross) == (Decimal("200.00"), Decimal("38.00"), Decimal("238.00"))
    html = render_document_html(invoice)
    assert "Arbeitskosten (§ 35a EStG): 238,00 €" in html and "38,00 €" in html


def test_document_discount_reduces_the_labour_share(shop):
    doc = draft(shop, shop.person, discount="10")
    line(doc, "100", "50")
    costs = labor_costs(finalize(doc))
    assert costs.net == Decimal("45.00") and costs.gross == Decimal("53.55")


def test_business_customers_and_quotes_get_no_note(shop):
    doc = draft(shop, shop.org)
    line(doc, "100", "100")
    assert "35a" not in render_document_html(finalize(doc))
    quote = create_draft(shop.company, Document.Type.QUOTE, shop.person, issue_date=DAY, service_date=DAY)
    line(quote, "100", "100")
    assert "35a" not in render_document_html(finalize(quote))


def test_item_share_is_copied_and_kept_on_delivery_notes(shop):
    item = create_item(shop.company, name="Fliesen legen", unit="HUR", net_price=Decimal("50"))
    item.labor_percent = Decimal("80")
    item.save()
    doc = draft(shop, shop.person)
    assert add_line_from_item(doc, item, quantity=Decimal("2")).labor_percent == Decimal("80")
    invoice = finalize(doc)
    note = create_delivery_note(invoice, issue_date=DAY)
    assert note.lines.get().labor_percent == Decimal("80")


@pytest.mark.parametrize("labor", ["-1", "100.01", "1e9"])
def test_out_of_range_shares_are_refused(shop, labor):
    with pytest.raises(DocumentError):
        line(draft(shop, shop.person), "1", labor)


def test_no_labour_means_no_note_and_zero(shop):
    doc = draft(shop, shop.person)
    line(doc, "100", "0")
    invoice = finalize(doc)
    assert labor_costs(invoice).gross == Decimal("0.00") and "35a" not in render_document_html(invoice)


def test_line_form_takes_the_share_and_rejects_garbage(client, shop):
    doc = draft(shop, shop.person)
    client.force_login(shop.user)
    base = {"text": "Montage", "quantity": "1", "unit_price": "100", "tax_treatment": "standard"}
    client.post(f"/dokumente/{doc.pk}/positionen/", {**base, "labor_percent": "75"})
    assert doc.lines.get().labor_percent == Decimal("75")
    for bad in ("101", "-5", "abc", "NaN", "1e999"):
        client.post(f"/dokumente/{doc.pk}/positionen/", {**base, "labor_percent": bad})
    assert doc.lines.count() == 1
