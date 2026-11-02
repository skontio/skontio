"""Bruttopreise: Nettopreis wird passend ermittelt, Unerreichbares wird gemeldet, nie mehr als angekündigt."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from skontio.core.models import Company
from skontio.documents.models import Document
from skontio.documents.services import (
    DocumentError,
    add_line,
    create_draft,
    finalize,
    gross_check,
    preview_totals,
    update_line,
)
from skontio.tax.engine import Regime, TaxTreatment, money
from skontio.tax.gross import net_unit_price
from skontio.taxstatus.services import start_regime

DAY = dt.date(2026, 3, 2)
D = Decimal


def draft(shop, customer=None):
    return create_draft(shop.company, Document.Type.INVOICE, customer or shop.person, issue_date=DAY, service_date=DAY)


def gross_line(doc, gross, quantity="1", treatment=TaxTreatment.STANDARD, discount="0"):
    return add_line(doc, text="Ware", quantity=D(quantity), unit_code="C62", gross_unit_price=D(gross),
                    tax_treatment=treatment, discount_percent=D(discount))  # fmt: skip


@pytest.mark.parametrize(
    ("gross", "quantity", "rate"),
    [("119", "1", "19"), ("10", "3", "19"), ("50", "2", "19"), ("12.34", "1.5", "7"), ("0.01", "1", "19")],
)
def test_reachable_amounts_are_hit_exactly(gross, quantity, rate):
    result = net_unit_price(D(gross), D(quantity), D(rate))
    assert result.exact and result.unit_price == result.unit_price.quantize(D("0.0001"))


def test_unreachable_amount_takes_the_lower_neighbour():
    result = net_unit_price(D("9.99"), D("1"), D("19"))  # 8,39 → 9,98 · 8,40 → 10,00
    assert not result.exact and result.reached == D("9.98")


@settings(max_examples=300, deadline=None)
@given(
    gross=st.decimals(min_value=D("0.01"), max_value=D("99999"), places=2),
    quantity=st.decimals(min_value=D("0.5"), max_value=D("500"), places=2),
    rate=st.sampled_from([D("19"), D("7"), D("0")]),
)
def test_never_more_than_a_cent_off_and_never_negative(gross, quantity, rate):
    result = net_unit_price(gross, quantity, rate)
    assert result.unit_price >= 0
    # a unit price has 4 decimals: with large quantities one step moves the line by more than a cent
    assert abs(result.reached - result.target) <= max(D("0.01"), money(quantity * D("0.0001") * (1 + rate / 100)))
    net = money(quantity * result.unit_price)
    assert net + money(net * rate / 100) == result.reached  # the same rounding as the invoice


def test_invoice_total_matches_the_gross_price(shop):
    doc = draft(shop)
    gross_line(doc, "50", quantity="2")
    assert preview_totals(doc).grand_total == D("100.00")
    invoice = finalize(doc)
    assert invoice.gross_total == D("100.00") and invoice.lines.get().gross_unit_price == D("50")


def test_reduced_rate_and_discount(shop):
    doc = draft(shop)
    gross_line(doc, "20", treatment=TaxTreatment.REDUCED, discount="10")
    assert preview_totals(doc).grand_total == D("18.00")


def test_small_business_gross_is_net(db):
    company = Company.objects.create(name="Klein", street="W", postcode="1", city="S", tax_number="1/2/3")
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2025, 1, 1))
    from skontio.crm.services import create_customer

    customer = create_customer(company, name="Privat", street="W", postcode="1", city="S")
    doc = create_draft(company, Document.Type.INVOICE, customer, issue_date=DAY, service_date=DAY)
    line = add_line(doc, text="X", quantity=D("1"), unit_code="C62", gross_unit_price=D("9.99"))
    assert line.unit_price == D("9.9900") and gross_check(line).exact


def test_quantity_change_keeps_the_gross_price_net_change_drops_it(shop):
    doc = draft(shop)
    line = gross_line(doc, "10")
    update_line(line, text="Ware", quantity=D("3"), unit_price=line.unit_price)
    assert line.gross_unit_price == D("10") and preview_totals(doc).grand_total == D("30.00")
    update_line(line, text="Ware", quantity=D("3"), unit_price=D("5"))
    assert line.gross_unit_price is None


def test_net_and_gross_together_or_neither_are_refused(shop):
    doc = draft(shop)
    with pytest.raises(DocumentError):
        add_line(doc, text="X", quantity=D("1"), unit_code="C62", unit_price=D("1"), gross_unit_price=D("1"))
    with pytest.raises(DocumentError):
        add_line(doc, text="X", quantity=D("1"), unit_code="C62")
    with pytest.raises(DocumentError):
        gross_line(doc, "-1")


def test_editor_warns_about_unreachable_amounts(client, shop):
    doc = draft(shop)
    client.force_login(shop.user)
    client.post(f"/dokumente/{doc.pk}/positionen/", {"text": "Ware", "quantity": "1", "gross_price": "9,99",
                                                     "tax_treatment": "standard"})  # fmt: skip
    page = client.get(f"/dokumente/{doc.pk}/").content.decode()
    assert "9,99 € brutto sind mit diesem Steuersatz nicht exakt möglich" in page and "9,98" in page


def test_editor_refuses_both_prices_and_garbage(client, shop):
    doc = draft(shop)
    client.force_login(shop.user)
    base = {"text": "Ware", "quantity": "1", "tax_treatment": "standard"}
    for extra in ({"gross_price": "10", "unit_price": "8"}, {"gross_price": "NaN"}, {"gross_price": "1e999"},
                  {"gross_price": "-3"}, {}):  # fmt: skip
        assert client.post(f"/dokumente/{doc.pk}/positionen/", {**base, **extra}).status_code in (200, 302)
    assert not doc.lines.exists()


def test_inline_edit_of_a_gross_line_changes_the_gross_price(client, shop):
    doc = draft(shop)
    line = gross_line(doc, "10")
    client.force_login(shop.user)
    assert 'name="gross_price"' in client.get(f"/dokumente/{doc.pk}/positionen/{line.pk}/").content.decode()
    client.post(f"/dokumente/{doc.pk}/positionen/{line.pk}/", {"text": "Ware", "quantity": "2", "gross_price": "15"})
    line.refresh_from_db()
    assert line.gross_unit_price == D("15") and preview_totals(doc).grand_total == D("30.00")
    client.post(f"/dokumente/{doc.pk}/positionen/{line.pk}/", {"text": "Ware", "quantity": "2", "unit_price": "5"})
    line.refresh_from_db()
    assert line.gross_unit_price is None and line.unit_price == D("5")


def test_nul_bytes_in_search_and_forms_do_not_crash(client, shop):
    client.force_login(shop.user)
    assert client.get("/suche/", {"q": "a\x00b"}).status_code == 200
    doc = draft(shop)
    client.post(f"/dokumente/{doc.pk}/positionen/", {"text": "Wa\x00re", "quantity": "1", "unit_price": "1",
                                                     "tax_treatment": "standard"})  # fmt: skip
    assert doc.lines.get().text == "Ware"
