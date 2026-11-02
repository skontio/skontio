"""Zusammenfassende Meldung: je USt-IdNr. und Art, Stornos negativ, Schlussrechnung ohne Doppelzählung."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import (
    add_line,
    cancel_invoice,
    create_draft,
    create_final_invoice,
    create_partial_invoice,
    finalize,
)
from skontio.reports.ustva import Period
from skontio.reports.zm import zm_report
from skontio.tax.engine import TaxTreatment

DAY = dt.date(2026, 2, 10)
Q1 = Period(2026, "Q1")


@pytest.fixture
def eu(shop):
    shop.company.vat_id = "DE123456789"
    shop.company.save()
    shop.at = create_customer(shop.company, name="Wien GmbH", street="R 1", postcode="1010", city="Wien",
                              country="AT", vat_id="ATU12345678")  # fmt: skip
    shop.gr = create_customer(shop.company, name="Athen AE", street="O 1", postcode="10", city="Athen",
                              country="GR", vat_id="EL123456789")  # fmt: skip
    return shop


def invoice(shop, customer, net, treatment, day=DAY, doc_type=Document.Type.INVOICE):
    draft = create_draft(shop.company, doc_type, customer, issue_date=day, service_date=day)
    add_line(draft, text="Leistung", quantity=Decimal("1"), unit_code="LS", unit_price=Decimal(net),
             tax_treatment=treatment)  # fmt: skip
    return finalize(draft)


def test_rows_per_vat_id_and_kind(eu):
    invoice(eu, eu.at, "1000.40", TaxTreatment.INTRA_EU_SUPPLY)
    invoice(eu, eu.at, "500", TaxTreatment.INTRA_EU_SUPPLY)
    invoice(eu, eu.at, "200", TaxTreatment.REVERSE_CHARGE)
    invoice(eu, eu.gr, "300", TaxTreatment.REVERSE_CHARGE)
    rows = {(r.country, r.vat_id, r.kind): r.amount for r in zm_report(eu.company, Q1).rows}
    assert rows == {
        ("AT", "U12345678", "L"): Decimal("1500"),
        ("AT", "U12345678", "S"): Decimal("200"),
        ("EL", "123456789", "S"): Decimal("300"),
    }


def test_domestic_sales_and_other_periods_are_not_reported(eu):
    invoice(eu, eu.org, "999", TaxTreatment.STANDARD)
    invoice(eu, eu.at, "100", TaxTreatment.INTRA_EU_SUPPLY, day=dt.date(2026, 4, 1))
    assert zm_report(eu.company, Q1).rows == []


def test_reverse_charge_outside_the_eu_is_not_reported(eu):
    ch = create_customer(eu.company, name="Zürich AG", street="B 1", postcode="8000", city="Zürich",
                         country="CH", vat_id="CHE123456789")  # fmt: skip
    invoice(eu, ch, "100", TaxTreatment.REVERSE_CHARGE)
    assert zm_report(eu.company, Q1).rows == []


def test_cancellation_in_the_same_period_cancels_out(eu):
    original = invoice(eu, eu.at, "400", TaxTreatment.INTRA_EU_SUPPLY)
    cancel_invoice(original, reason="falsche Menge", issue_date=DAY)
    assert zm_report(eu.company, Q1).rows == []


def test_cancellation_in_a_later_period_is_negative_there(eu):
    original = invoice(eu, eu.at, "400", TaxTreatment.INTRA_EU_SUPPLY)
    cancel_invoice(original, reason="Rückgabe", issue_date=dt.date(2026, 4, 2))
    (row,) = zm_report(eu.company, Period(2026, "Q2")).rows
    assert row.amount == Decimal("-400")


def test_vat_id_from_the_invoice_not_the_changed_customer(eu):
    invoice(eu, eu.at, "100", TaxTreatment.INTRA_EU_SUPPLY)
    eu.at.vat_id = "ATU99999999"
    eu.at.save()
    (row,) = zm_report(eu.company, Q1).rows
    assert row.full_vat_id == "ATU12345678"


def test_page_lists_rows_and_survives_garbage(client, eu):
    invoice(eu, eu.at, "100", TaxTreatment.INTRA_EU_SUPPLY)
    client.force_login(eu.user)
    page = client.get("/auswertungen/zm/", {"jahr": "2026", "zeitraum": "Q1"}).content.decode()
    assert "U12345678" in page and "27.04.2026" in page  # 25.04.2026 is a Saturday
    for params in ({"zeitraum": "J"}, {"zeitraum": "Q9"}, {"jahr": "x" * 50}, {"zeitraum": "<script>"}):
        assert client.get("/auswertungen/zm/", params).status_code == 200


def test_final_invoice_reports_only_what_the_partial_did_not(eu):
    quote = invoice(eu, eu.at, "1000", TaxTreatment.INTRA_EU_SUPPLY, doc_type=Document.Type.QUOTE)
    finalize(create_partial_invoice(quote, issue_date=DAY, service_date=DAY, percent=Decimal("30")))
    final = create_final_invoice(quote, issue_date=dt.date(2026, 3, 20), service_date=dt.date(2026, 3, 20))
    final = finalize(final)
    (row,) = zm_report(eu.company, Q1).rows
    assert row.amount == Decimal("1000") and final.deductions.exists()
