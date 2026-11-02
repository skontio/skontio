"""Positionsgruppen: Überschrift und Zwischensumme, Gruppen bleiben zusammen, Steuer weiter je Position."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.documents.models import Document
from skontio.documents.renditions import render_document_html
from skontio.documents.services import (
    DocumentError,
    add_line,
    convert_quote_to_invoice,
    create_draft,
    finalize,
    preview_totals,
    update_line,
)
from skontio.einvoice.cii import Profile, build_cii
from skontio.einvoice.mapping import invoice_from_document
from skontio.tax.engine import TaxTreatment

DAY = dt.date(2026, 3, 2)
D = Decimal


def draft(shop, doc_type=Document.Type.INVOICE):
    return create_draft(shop.company, doc_type, shop.org, issue_date=DAY, service_date=DAY)


def line(doc, text, price, group="", treatment=TaxTreatment.STANDARD):
    return add_line(doc, text=text, quantity=D("1"), unit_code="C62", unit_price=D(price), group=group,
                    tax_treatment=treatment)  # fmt: skip


def order(doc):
    return [(line.position, line.text, line.group) for line in doc.lines.all()]


def test_new_line_joins_its_group_and_positions_are_renumbered(shop):
    doc = draft(shop)
    line(doc, "Holz", "100", "Material")
    line(doc, "Montage", "50", "Arbeit")
    line(doc, "Schrauben", "10", "Material")
    line(doc, "Anfahrt", "20")
    assert order(doc) == [
        (1, "Holz", "Material"), (2, "Schrauben", "Material"), (3, "Montage", "Arbeit"), (4, "Anfahrt", ""),
    ]  # fmt: skip


def test_changing_the_group_moves_the_line(shop):
    doc = draft(shop)
    wood = line(doc, "Holz", "100", "Material")
    line(doc, "Montage", "50", "Arbeit")
    update_line(wood, text="Holz", quantity=D("1"), unit_price=D("100"), group="Arbeit")
    assert order(doc) == [(1, "Montage", "Arbeit"), (2, "Holz", "Arbeit")]
    update_line(wood, text="Holz", quantity=D("1"), unit_price=D("100"), group="")
    assert order(doc) == [(1, "Montage", "Arbeit"), (2, "Holz", "")]
    update_line(wood, text="Holz neu", quantity=D("2"), unit_price=D("100"))  # group not given: stays
    assert order(doc) == [(1, "Montage", "Arbeit"), (2, "Holz neu", "")]


def test_group_names_are_trimmed_and_limited(shop):
    doc = draft(shop)
    assert line(doc, "X", "1", "  Material \n  vor Ort ").group == "Material vor Ort"
    with pytest.raises(DocumentError):
        line(doc, "Y", "1", "x" * 201)


def test_groups_do_not_change_taxes_or_totals(shop):
    plain, grouped = draft(shop), draft(shop)
    for doc, groups in ((plain, ("", "")), (grouped, ("Bücher", "Beratung"))):
        line(doc, "Buch", "10.01", groups[0], TaxTreatment.REDUCED)
        line(doc, "Beratung", "33.33", groups[1])
    assert preview_totals(plain) == preview_totals(grouped)


def test_pdf_shows_headings_and_subtotals(shop):
    doc = draft(shop)
    line(doc, "Holz", "100", "Material")
    line(doc, "Schrauben", "10.50", "Material")
    line(doc, "Montage", "50", "Arbeit")
    line(doc, "Anfahrt", "20")
    html = render_document_html(finalize(doc))
    assert html.count('class="group-heading"') == 2 and html.count('class="group-subtotal"') == 2
    assert "Zwischensumme Material" in html and "110,50" in html and "Zwischensumme Arbeit" in html
    assert html.index("Material") < html.index("Holz") < html.index("Zwischensumme Material") < html.index("Montage")


def test_e_invoice_has_no_groups_and_unchanged_sums(shop):
    doc = draft(shop)
    line(doc, "Holz", "100", "Material")
    line(doc, "Montage", "50", "Arbeit")
    invoice = finalize(doc)
    xml = build_cii(invoice_from_document(invoice), Profile.EN16931).decode()
    assert "Material" not in xml and invoice.net_total == D("150.00")


def test_html_in_group_names_is_escaped(shop):
    doc = draft(shop)
    line(doc, "Holz", "1", "<script>alert(1)</script>")
    html = render_document_html(finalize(doc))
    assert "<script>alert" not in html and "&lt;script&gt;" in html


def test_groups_are_copied_to_the_invoice(shop):
    quote = draft(shop, Document.Type.QUOTE)
    line(quote, "Holz", "100", "Material")
    quote = finalize(quote)
    invoice = convert_quote_to_invoice(quote, issue_date=DAY, service_date=DAY)
    assert invoice.lines.get().group == "Material"


def test_editor_adds_groups_and_keeps_the_group_for_the_next_line(client, shop):
    doc = draft(shop)
    client.force_login(shop.user)
    url = f"/dokumente/{doc.pk}/positionen/"
    base = {"quantity": "1", "unit_price": "10", "tax_treatment": "standard"}
    page = client.post(url, {**base, "text": "Holz", "group": "Material"}, HTTP_HX_REQUEST="true").content.decode()
    assert 'value="Material"' in page and "Zwischensumme Material" in page
    client.post(url, {**base, "text": "Montage", "group": "Arbeit"})
    client.post(url, {**base, "text": "Schrauben", "group": "Material"})
    assert order(doc)[1] == (2, "Schrauben", "Material")
    page = client.get(f"/dokumente/{doc.pk}/").content.decode()
    assert '<option value="Arbeit">' in page and page.count('class="group-subtotal"') == 2


def test_editor_inline_edit_changes_group_and_refuses_garbage(client, shop):
    doc = draft(shop)
    first = line(doc, "Holz", "10", "Material")
    line(doc, "Montage", "10", "Arbeit")
    client.force_login(shop.user)
    url = f"/dokumente/{doc.pk}/positionen/{first.pk}/"
    assert 'name="group"' in client.get(url).content.decode()
    client.post(url, {"text": "Holz", "quantity": "1", "unit_price": "10", "group": "Arbeit"})
    assert order(doc) == [(1, "Montage", "Arbeit"), (2, "Holz", "Arbeit")]
    garbage = {"text": "Holz", "quantity": "1", "unit_price": "10", "group": "x" * 5000}
    assert client.post(url, garbage).status_code == 200
    first.refresh_from_db()
    assert first.group == "Arbeit"


def test_finalized_page_shows_groups(client, shop):
    doc = draft(shop)
    line(doc, "Holz", "10", "Material")
    invoice = finalize(doc)
    client.force_login(shop.user)
    page = client.get(f"/dokumente/{invoice.pk}/").content.decode()
    assert "Zwischensumme Material" in page
