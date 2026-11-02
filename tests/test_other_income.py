"""Sonstige Einnahmen ohne Rechnung: EÜR, Umsatzsteuer, DATEV, § 19-Grenze, Storno, Müll und Rollen."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile

from skontio.accounting.datev import bookings
from skontio.accounts.roles import DRAFTS_ONLY_GROUP
from skontio.core.models import Company
from skontio.expenses.income import IncomeError, cancel_income, create_income
from skontio.expenses.models import OtherIncome
from skontio.reports.euer import euer, journal
from skontio.reports.ustva import Period, vat_report
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime
from skontio.taxstatus.thresholds import net_receipts

DAY = dt.date(2026, 3, 2)
CAT = OtherIncome.Category


def income(company, **kwargs):
    fields = {"received_on": DAY, "payer": "Plattform GmbH", "gross_amount": Decimal("119.00"),
              "vat_rate": Decimal("19"), "category": CAT.PLATFORM} | kwargs  # fmt: skip
    return create_income(company, **fields)


@pytest.fixture
def small(db):
    company = Company.objects.create(name="Klein", street="W", postcode="1", city="S", tax_number="1/2/3")
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2025, 1, 1))
    return company


# ---- Regelbesteuerung --------------------------------------------------------------------------


def test_standard_rate_income_flows_into_euer_ustva_and_datev(shop):
    entry = income(shop.company)
    assert entry.number.startswith("EI-2026-")
    report = euer(shop.company, 2026)
    lines = {line.key: line.amount for line in report.income}
    assert lines["taxable"] == Decimal("100.00") and lines["collected_vat"] == Decimal("19.00")
    assert [e.number for e in journal(shop.company, 2026)] == [entry.number]
    assert vat_report(shop.company, Period(2026, "Q1")).values["81"] == Decimal("100")
    (booking,) = bookings(shop.company, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert (booking.amount, booking.account, booking.counter_account) == (Decimal("119.00"), "1200", "8400")


def test_reduced_and_zero_rates(shop):
    income(shop.company, gross_amount=Decimal("107.00"), vat_rate=Decimal("7"))
    income(shop.company, gross_amount=Decimal("50.00"), vat_rate=Decimal("0"), category=CAT.INTEREST)
    lines = {line.key: line.amount for line in euer(shop.company, 2026).income}
    assert lines["taxable"] == Decimal("100.00") and lines["tax_free"] == Decimal("50.00")
    report = vat_report(shop.company, Period(2026, "Q1"))
    assert report.values["86"] == Decimal("100") and "81" not in report.values
    assert any("ohne Umsatzsteuer sind nicht enthalten" in note for note in report.notes)
    accounts = sorted(b.counter_account for b in bookings(shop.company, dt.date(2026, 1, 1), dt.date(2026, 12, 31)))
    assert accounts == ["8100", "8300"]


def test_cancelled_income_disappears_everywhere(shop):
    entry = income(shop.company)
    cancel_income(entry, reason="doppelt erfasst")
    assert euer(shop.company, 2026).income == []
    assert not vat_report(shop.company, Period(2026, "Q1")).values.get("81")
    assert bookings(shop.company, dt.date(2026, 1, 1), dt.date(2026, 12, 31)) == []
    with pytest.raises(IncomeError):
        cancel_income(entry, reason="nochmal")


# ---- Kleinunternehmer und § 19-Grenze ----------------------------------------------------------


def test_small_business_may_not_show_vat(small):
    with pytest.raises(IncomeError, match="Kleinunternehmer"):
        income(small)
    entry = income(small, vat_rate=Decimal("0"))
    lines = {line.key: line.amount for line in euer(small, 2026).income}
    assert lines == {"small_business": Decimal("119.00")}
    (booking,) = bookings(small, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert booking.counter_account == "8195" and booking.text.startswith(entry.payer)


def test_only_turnover_counts_towards_the_small_business_limit(small):
    income(small, vat_rate=Decimal("0"), gross_amount=Decimal("100"))
    income(small, vat_rate=Decimal("0"), gross_amount=Decimal("5000"), category=CAT.ASSET_SALE)
    income(small, vat_rate=Decimal("0"), gross_amount=Decimal("70"), category=CAT.REFUND)
    income(small, vat_rate=Decimal("0"), gross_amount=Decimal("900"), category=CAT.SUBSIDY)
    assert sum(amount for _, amount in net_receipts(small, 2026)) == Decimal("100.00")


# ---- Müll --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"gross_amount": Decimal("-5")},
        {"gross_amount": Decimal("0")},
        {"gross_amount": Decimal("NaN")},
        {"gross_amount": Decimal("Infinity")},
        {"gross_amount": Decimal("1e30")},
        {"gross_amount": Decimal("1.005")},
        {"vat_rate": Decimal("16")},
        {"vat_rate": Decimal("-19")},
        {"category": "lottery"},
        {"method": "bitcoin"},
        {"payer": "   "},
        {"received_on": dt.date(2099, 1, 1)},
    ],
)
def test_garbage_is_refused_and_nothing_is_stored(shop, fields):
    with pytest.raises(IncomeError):
        income(shop.company, **fields)
    assert not OtherIncome.objects.exists()


def test_unknown_fields_cannot_be_smuggled_in(shop):
    with pytest.raises(IncomeError, match="Unbekannte Felder"):
        income(shop.company, number="EI-FAKE", cancelled_at=None)


def test_executable_receipt_is_refused(shop):
    with pytest.raises(IncomeError):
        income(shop.company, receipt=b"MZ\x90\x00 evil", receipt_filename="abrechnung.pdf")
    assert not OtherIncome.objects.exists()


# ---- Oberfläche und Rollen ---------------------------------------------------------------------

FORM = {"received_on": "2026-03-02", "payer": "Plattform", "category": "platform", "gross_amount": "119,00",
        "vat_rate": "19", "method": "bank"}  # fmt: skip


def test_create_list_detail_and_cancel_through_the_ui(client, shop):
    client.force_login(shop.user)
    pdf = SimpleUploadedFile("abrechnung.pdf", b"%PDF-1.4 Abrechnung", content_type="application/pdf")
    response = client.post("/einnahmen/neu/", {**FORM, "receipt": pdf})
    entry = OtherIncome.objects.get()
    assert response.status_code == 302 and response["Location"] == f"/einnahmen/{entry.pk}/"
    assert entry.receipt_id and entry.gross_amount == Decimal("119.00")
    assert entry.number in client.get("/einnahmen/", {"jahr": "2026"}).content.decode()
    assert "100,00" in client.get(f"/einnahmen/{entry.pk}/").content.decode()
    assert client.get(f"/einnahmen/{entry.pk}/beleg/").status_code == 200
    client.post(f"/einnahmen/{entry.pk}/stornieren/", {"reason": ""})
    entry.refresh_from_db()
    assert entry.cancelled_at is None
    client.post(f"/einnahmen/{entry.pk}/stornieren/", {"reason": "Fehlbuchung"})
    entry.refresh_from_db()
    assert entry.cancelled_at is not None


def test_form_errors_are_shown_not_crashed(client, shop):
    client.force_login(shop.user)
    for bad in ({"gross_amount": "abc"}, {"vat_rate": "16"}, {"received_on": "2026-02-30"}, {"payer": ""}):
        response = client.post("/einnahmen/neu/", {**FORM, **bad})
        assert response.status_code == 200
    assert not OtherIncome.objects.exists()


def test_drafts_only_may_record_but_not_cancel(client, shop, django_user_model):
    entry = income(shop.company)
    helper = django_user_model.objects.create_user("hilfe")
    helper.groups.add(Group.objects.get_or_create(name=DRAFTS_ONLY_GROUP)[0])
    client.force_login(helper)
    assert client.post(f"/einnahmen/{entry.pk}/stornieren/", {"reason": "x"}).status_code == 403
    entry.refresh_from_db()
    assert entry.cancelled_at is None


def test_other_company_income_is_not_reachable(client, shop):
    other = Company.objects.create(name="Fremd", street="X", postcode="1", city="Y")
    start_regime(other, Regime.STANDARD, dt.date(2024, 1, 1))
    foreign = income(other)
    client.force_login(shop.user)
    for url in (f"/einnahmen/{foreign.pk}/", f"/einnahmen/{foreign.pk}/beleg/"):
        assert client.get(url).status_code == 404
    assert client.post(f"/einnahmen/{foreign.pk}/stornieren/", {"reason": "x"}).status_code == 404
    assert foreign.number not in client.get("/einnahmen/", {"jahr": "2026"}).content.decode()
