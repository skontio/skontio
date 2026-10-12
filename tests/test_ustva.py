"""Umsatzsteuer-Voranmeldung: Kennzahlen nach Istversteuerung, Vorsteuer, § 13b, Frist, Übermittlung."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.expenses.services import create_expense
from skontio.payments.services import record_payment
from skontio.reports.ustva import Period, SollNotSupported, periods, vat_report
from skontio.tax.engine import Regime, TaxTreatment
from skontio.taxstatus.services import start_regime

D = Decimal
Q3 = Period(2026, "Q3")


def paid_invoice(shop, *, net: str, item=None, paid: str | None = None, on=dt.date(2026, 8, 1)):
    doc = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=on, service_date=on)
    add_line_from_item(
        doc, item or create_item(shop.company, name="Leistung", unit="LS", net_price=D(net)), quantity=D("1")
    )
    doc = finalize(doc)
    record_payment(doc, D(paid) if paid else doc.gross_total, on)
    return doc


def expense(company, **kwargs):
    fields = {
        "receipt_date": dt.date(2026, 8, 5),
        "paid_on": dt.date(2026, 8, 5),
        "supplier": "Lieferant",
        "gross_amount": D("119"),
        "vat_rate": D("19"),
    } | kwargs
    return create_expense(company, **fields)


class TestStandard:
    def test_outputs_inputs_and_payable(self, shop):
        paid_invoice(shop, net="1000")  # 19 %
        books = create_item(
            shop.company, name="Buch", unit="C62", net_price=D("100"), tax_treatment=TaxTreatment.REDUCED
        )
        paid_invoice(shop, net="100", item=books)  # 7 %
        paid_invoice(shop, net="500", paid="297.50")  # half paid → half of 500 net
        expense(shop.company)  # 19 € Vorsteuer
        report = vat_report(shop.company, Q3)
        assert report.values["81"] == D("1250") and report.values["86"] == D("100")
        assert report.values["66"] == D("19.00")
        assert report.values["83"] == D("1250") * D("0.19") + D("7") - D("19.00")
        assert not report.small_business

    def test_payments_outside_the_period_do_not_count(self, shop):
        paid_invoice(shop, net="1000", on=dt.date(2026, 6, 30))
        assert vat_report(shop.company, Q3).values.get("81") is None

    def test_reverse_charge_eu_and_third_country(self, shop):
        expense(shop.company, supplier="Ads Ireland", supplier_country="IE", reverse_charge=True, gross_amount=D("100"))
        expense(shop.company, supplier="SaaS Inc", supplier_country="US", reverse_charge=True, gross_amount=D("50"))
        values = vat_report(shop.company, Q3).values
        assert (values["46"], values["47"], values["84"], values["85"]) == (D("100"), D("19.00"), D("50"), D("9.50"))
        assert values["67"] == D("28.50") and values["83"] == D("0.00")  # owed and deducted at once

    def test_sollversteuerung_is_refused(self, shop):
        shop.company.vat_basis = "soll"
        shop.company.save()
        with pytest.raises(SollNotSupported):
            vat_report(shop.company, Q3)


def test_small_business_owes_reverse_charge_without_deduction(db):
    company = Company.objects.create(name="Klein", tax_number="1/2/3")
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2026, 1, 1))
    expense(company, supplier="Ads Ireland", supplier_country="IE", reverse_charge=True, gross_amount=D("100"))
    expense(company)  # normal expense: no Vorsteuer for Kleinunternehmer
    report = vat_report(company, Q3)
    assert report.values == {"46": D("100"), "47": D("19.00"), "83": D("19.00")}
    assert report.small_business and "§ 13b" in report.notes[0]


class TestPeriods:
    def test_deadline_moves_to_monday_and_extension(self):
        assert Q3.deadline(extension=False) == dt.date(2026, 10, 12)  # 10 Oct 2026 is a Saturday
        assert Q3.deadline(extension=True) == dt.date(2026, 11, 10)

    def test_monthly_and_quarterly(self):
        assert [p.code for p in periods(2026, "quarterly")] == ["Q1", "Q2", "Q3", "Q4"]
        months = periods(2026, "monthly")
        assert len(months) == 12 and months[1].end == dt.date(2026, 2, 28)
        assert Q3.start == dt.date(2026, 7, 1) and Q3.end == dt.date(2026, 9, 30)


class TestWeb:
    def test_page_copy_values_submit_and_differences(self, client, shop):
        paid_invoice(shop, net="1000")
        client.force_login(shop.user)
        url = "/auswertungen/ustva/?jahr=2026&zeitraum=Q3"
        page = client.get(url).content.decode()
        assert "<strong>81</strong>" in page and 'data-copy="1000"' in page
        assert "Zahllast: 190,00 €" in page and "12.10.2026" in page
        client.post("/auswertungen/ustva/uebermittelt/", {"jahr": "2026", "zeitraum": "Q3"})
        page = client.get(url).content.decode()
        assert "übermittelt vermerkt" in page and "Seit der Übermittlung geändert" not in page
        paid_invoice(shop, net="100")
        page = client.get(url).content.decode()
        assert "Seit der Übermittlung geändert" in page and "Kz 81: 1000 → 1100" in page

    def test_soll_shows_a_message(self, client, shop):
        shop.company.vat_basis = "soll"
        shop.company.save()
        client.force_login(shop.user)
        assert "Sollversteuerung wird noch nicht unterstützt" in client.get("/auswertungen/ustva/").content.decode()
