"""Frei wählbare Berichtszeiträume (EÜR, Journal) und Umsatzsteuer-Jahresübersicht."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.payments.services import record_payment
from skontio.reports.euer import euer, journal
from skontio.reports.ustva import Period, parse_period, vat_report
from skontio.taxstatus.models import VatReturn


def paid_invoice(shop, day):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=day, service_date=day)
    add_line_from_item(draft, shop.item, quantity=Decimal("1"))  # 80 € net, 19 %
    invoice = finalize(draft)
    record_payment(invoice, invoice.gross_total, day)
    return invoice


@pytest.fixture
def two_payments(shop):
    return paid_invoice(shop, dt.date(2026, 3, 2)), paid_invoice(shop, dt.date(2026, 5, 10))


def test_euer_for_a_custom_range_counts_only_payments_inside(shop, two_payments):
    march = euer(shop.company, start=dt.date(2026, 3, 1), end=dt.date(2026, 3, 31))
    assert march.income_total == Decimal("95.20") and not march.whole_year
    assert "Frei gewählter Zeitraum" in march.notes[0]
    year = euer(shop.company, 2026)
    assert year.income_total == Decimal("190.40") and year.whole_year
    may = journal(shop.company, start=dt.date(2026, 5, 10), end=dt.date(2026, 5, 10))
    assert [e.number for e in may] == [two_payments[1].number]


def test_range_spanning_two_years(shop):
    paid_invoice(shop, dt.date(2025, 12, 31))
    paid_invoice(shop, dt.date(2026, 3, 2))
    report = euer(shop.company, start=dt.date(2025, 12, 1), end=dt.date(2026, 3, 2))
    assert report.income_total == Decimal("190.40")


def test_reversed_range_is_refused(shop):
    with pytest.raises(ValueError):
        euer(shop.company, start=dt.date(2026, 2, 1), end=dt.date(2026, 1, 1))


def test_euer_page_and_csv_use_the_range(client, shop, two_payments):
    client.force_login(shop.user)
    page = client.get("/auswertungen/euer/", {"von": "2026-03-01", "bis": "2026-03-31"}).content.decode()
    assert "01.03.2026 – 31.03.2026" in page and "95,20" in page and "190,40" not in page
    response = client.get("/auswertungen/euer.csv", {"von": "2026-03-01", "bis": "2026-03-31"})
    body = response.content.decode()
    assert 'filename="euer-2026-03-01-bis-2026-03-31.csv"' in response["Content-Disposition"]
    assert two_payments[0].number in body and two_payments[1].number not in body


@pytest.mark.parametrize(
    "params",
    [
        {"von": "2026-04-01", "bis": "2026-03-01"},  # reversed → whole year
        {"von": "2026-02-30", "bis": "2026-03-01"},
        {"von": "<script>", "bis": "x"},
        {"von": "0001-01-01", "bis": "9999-12-31"},
        {"jahr": "abc"},
        {"jahr": "99999999999999999999"},
    ],
)
def test_garbage_ranges_never_crash(client, shop, two_payments, params):
    client.force_login(shop.user)
    assert client.get("/auswertungen/euer/", params).status_code == 200
    assert client.get("/auswertungen/euer.csv", params).status_code == 200


# ---- Umsatzsteuer-Jahresübersicht --------------------------------------------------------------


def test_annual_period_covers_the_whole_year():
    period = parse_period(2026, "J")
    assert (period.start, period.end, period.label, period.annual) == (
        dt.date(2026, 1, 1), dt.date(2026, 12, 31), "Jahr 2026", True
    )  # fmt: skip


def test_annual_report_sums_all_quarters(shop, two_payments):
    q1 = vat_report(shop.company, Period(2026, "Q1")).values
    q2 = vat_report(shop.company, Period(2026, "Q2")).values
    year = vat_report(shop.company, Period(2026, "J"))
    assert year.values["81"] == q1["81"] + q2["81"] == Decimal("160")
    assert year.values["83"] == q1["83"] + q2["83"]
    assert any("Jahresübersicht" in note for note in year.notes)


def test_annual_page_shows_prepayments_and_cannot_be_submitted(client, shop, two_payments):
    client.force_login(shop.user)
    client.post("/auswertungen/ustva/uebermittelt/", {"jahr": "2026", "zeitraum": "Q1"})
    page = client.get("/auswertungen/ustva/", {"jahr": "2026", "zeitraum": "J"}).content.decode()
    assert "Umsatzsteuer-Jahresübersicht 2026" in page and "Summe Kz 83: 15,20" in page
    assert "Als übermittelt vermerken" not in page
    response = client.post("/auswertungen/ustva/uebermittelt/", {"jahr": "2026", "zeitraum": "J"})
    assert response.status_code == 400 and not VatReturn.objects.filter(period="J").exists()


@pytest.mark.parametrize("code", ["j", "JJ", "Q5", "M13", "", "J; DROP"])
def test_unknown_period_codes_fall_back(client, shop, code):
    client.force_login(shop.user)
    assert client.get("/auswertungen/ustva/", {"jahr": "2026", "zeitraum": code}).status_code == 200


def test_marking_a_past_period_keeps_its_year(client, shop):
    """The form posts jahr/zeitraum in the body; the year must come from there, not default to today."""
    client.force_login(shop.user)
    client.post("/auswertungen/ustva/uebermittelt/", {"jahr": "2024", "zeitraum": "Q4"})
    assert VatReturn.objects.filter(year=2024, period="Q4").exists()
