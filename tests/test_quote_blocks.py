"""Angebot mit laufenden Positionen: getrennte Summen einmalig / monatlich statt einer gemischten Summe."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from skontio.documents.models import Document
from skontio.documents.renditions import render_document_html
from skontio.documents.services import add_line, create_draft, finalize

DAY = dt.date(2026, 3, 2)


def quote(shop, doc_type=Document.Type.QUOTE):
    return create_draft(shop.company, doc_type, shop.org, issue_date=DAY, service_date=DAY)


def line(doc, price, unit, qty="1", text="Position"):
    return add_line(doc, text=text, quantity=Decimal(qty), unit_code=unit, unit_price=Decimal(price))


def test_quote_splits_one_time_and_monthly_totals(shop):
    doc = quote(shop)
    line(doc, "500", "LS", text="Einrichtung")
    line(doc, "30", "MON", qty="12", text="Hosting")
    html = render_document_html(finalize(doc))
    assert "Einmalig" in html and "Monatlich (pro Monat)" in html
    assert "595,00 €" in html  # 500 + 19 %
    assert "35,70 €" in html  # 30 + 19 % per month, not × 12


def test_quote_without_recurring_lines_keeps_single_total(shop):
    doc = quote(shop)
    line(doc, "500", "LS")
    html = render_document_html(finalize(doc))
    assert "Einmalig" not in html and "Monatlich" not in html


def test_invoice_is_never_split(shop):
    doc = quote(shop, Document.Type.INVOICE)
    line(doc, "500", "LS")
    line(doc, "30", "MON")
    html = render_document_html(finalize(doc))
    assert "Monatlich (pro Monat)" not in html
