"""DATEV-Export (EXTF Buchungsstapel): Format, Konten, Aufteilung, Grenzen und Angriffe."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group

from skontio.accounting.datev import DatevError, bookings, export, filename
from skontio.accounting.datev_columns import COLUMNS
from skontio.accounting.models import AccountAssignment, LedgerSettings
from skontio.accounts.roles import DRAFTS_ONLY_GROUP
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line, create_draft, finalize
from skontio.expenses.services import cancel_expense, create_expense
from skontio.payments.models import Payment
from skontio.payments.services import record_payment, reverse_payment
from skontio.tax.engine import Regime, TaxTreatment
from skontio.taxstatus.services import start_regime

YEAR = 2026
DAY = dt.date(YEAR, 3, 10)
START, END = dt.date(YEAR, 1, 1), dt.date(YEAR, 12, 31)
ADDRESS = {"street": "Weg 1", "postcode": "12345", "city": "Stadt"}


def invoice(company, customer, lines, day=DAY):
    draft = create_draft(company, Document.Type.INVOICE, customer, issue_date=day, service_date=day)
    for price, treatment in lines:
        add_line(
            draft, text="Leistung", quantity=Decimal("1"), unit_code="C62", unit_price=Decimal(price),
            tax_treatment=treatment,
        )  # fmt: skip
    return finalize(draft)


@pytest.fixture
def ledger(shop):
    LedgerSettings.objects.create(company=shop.company, consultant_number=12345, client_number=678)
    return shop


def parse(data: bytes) -> tuple[list[str], list[str], list[list[str]]]:
    text = data.decode("cp1252")
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")[:-1]
    return lines[0].split(";"), lines[1].split(";"), [line.split(";") for line in lines[2:]]


class TestFormat:
    def test_header_columns_and_one_booking(self, ledger):
        record_payment(invoice(ledger.company, ledger.org, [("100", TaxTreatment.STANDARD)]), Decimal("119"), DAY)
        head, columns, rows = parse(export(ledger.company, START, END, created=dt.datetime(2026, 4, 1, 12, 0)))
        assert head[:6] == ['"EXTF"', "700", "21", '"Buchungsstapel"', "13", "20260401120000000"]
        assert head[10:16] == ["12345", "678", "20260101", "4", "20260101", "20261231"]
        assert head[21] == '"EUR"' and head[26] == '"03"' and len(head) == 31
        assert columns == list(COLUMNS) and len(columns) == 125
        (row,) = rows
        assert len(row) == 125
        assert row[0:3] == ["119,00", '"S"', '"EUR"']
        assert (row[6], row[7], row[8], row[9]) == ("1200", "8400", "", "1003")
        assert row[10] == f'"RE-{YEAR}-0001"'

    def test_skr04_and_own_accounts(self, ledger):
        LedgerSettings.objects.filter(company=ledger.company).update(chart="skr04")
        AccountAssignment.objects.create(company=ledger.company, key="bank", account="1810")
        record_payment(invoice(ledger.company, ledger.org, [("100", TaxTreatment.STANDARD)]), Decimal("119"), DAY)
        head, _, (row,) = parse(export(ledger.company, START, END))
        assert head[26] == '"04"' and (row[6], row[7]) == ("1810", "4400")

    def test_umlauts_are_windows_1252_and_quotes_are_doubled(self, ledger):
        customer = create_customer(ledger.company, name='Müller "Bau" GmbH', **ADDRESS)
        record_payment(invoice(ledger.company, customer, [("100", TaxTreatment.STANDARD)]), Decimal("119"), DAY)
        data = export(ledger.company, START, END)
        assert "Müller".encode("cp1252") in data
        assert '"Müller ""Bau"" GmbH' in data.decode("cp1252")

    def test_filename(self):
        assert filename(START, END) == "EXTF_Buchungsstapel_20260101_20261231.csv"


class TestBookings:
    def test_mixed_rates_are_split_and_add_up_to_the_payment(self, ledger):
        doc = invoice(ledger.company, ledger.org, [("100", TaxTreatment.STANDARD), ("50", TaxTreatment.REDUCED)])
        record_payment(doc, Decimal("100"), DAY)  # partial payment of 172,50
        rows = bookings(ledger.company, START, END)
        assert {r.counter_account for r in rows} == {"8400", "8300"}
        assert sum(r.amount for r in rows) == Decimal("100.00")
        assert all(r.side == "S" and r.bu_key == "" for r in rows)

    def test_reversed_payment_is_booked_the_other_way(self, ledger):
        doc = invoice(ledger.company, ledger.org, [("100", TaxTreatment.STANDARD)])
        payment = record_payment(doc, Decimal("119"), DAY)
        reverse_payment(payment, reason="Fehlbuchung", on=DAY)
        rows = bookings(ledger.company, START, END)
        assert sorted((r.side, r.amount) for r in rows) == [("H", Decimal("119.00")), ("S", Decimal("119.00"))]

    def test_cash_and_paypal_use_their_own_money_accounts(self, ledger):
        doc = invoice(ledger.company, ledger.org, [("100", TaxTreatment.STANDARD)])
        record_payment(doc, Decimal("19"), DAY, method=Payment.Method.CASH)
        record_payment(doc, Decimal("100"), DAY, method=Payment.Method.PAYPAL)
        assert {r.account for r in bookings(ledger.company, START, END)} == {"1000", "1360"}

    def test_expenses_with_input_vat_keys(self, ledger):
        common = {"receipt_date": DAY, "paid_on": DAY, "supplier": "Hosting GmbH", "category": "software"}
        create_expense(ledger.company, gross_amount=Decimal("119"), vat_rate=Decimal("19"), **common)
        create_expense(ledger.company, gross_amount=Decimal("10.70"), vat_rate=Decimal("7"), **common)
        create_expense(ledger.company, gross_amount=Decimal("5"), vat_rate=Decimal("0"), **common)
        rows = bookings(ledger.company, START, END)
        assert sorted(r.bu_key for r in rows) == ["", "8", "9"]
        assert {(r.account, r.counter_account, r.side) for r in rows} == {("4806", "1200", "S")}

    def test_reverse_charge_expense_is_flagged_without_key(self, ledger):
        create_expense(
            ledger.company, receipt_date=DAY, paid_on=DAY, supplier="Ads Ltd", supplier_country="IE",
            gross_amount=Decimal("50"), vat_rate=Decimal("19"), reverse_charge=True, category="advertising",
        )  # fmt: skip
        (row,) = bookings(ledger.company, START, END)
        assert row.review and row.bu_key == "" and row.text.startswith("§13b prüfen")

    def test_cancelled_unpaid_and_other_year_expenses_are_left_out(self, ledger):
        common = {"supplier": "X", "gross_amount": Decimal("10"), "vat_rate": Decimal("19")}
        cancel_expense(create_expense(ledger.company, receipt_date=DAY, paid_on=DAY, **common), reason="doppelt")
        create_expense(ledger.company, receipt_date=DAY, paid_on=None, **common)
        create_expense(
            ledger.company, receipt_date=dt.date(YEAR - 1, 12, 1), paid_on=dt.date(YEAR - 1, 12, 30), **common
        )
        assert bookings(ledger.company, START, END) == []

    def test_small_business_books_gross_on_its_own_revenue_account_without_keys(self, db):
        company = Company.objects.create(name="Klein", tax_number="1/2/3", iban="DE02120300000000202051", **ADDRESS)
        start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
        LedgerSettings.objects.create(company=company, consultant_number=1001, client_number=1)
        customer = create_customer(company, name="Kunde", **ADDRESS)
        record_payment(invoice(company, customer, [("100", TaxTreatment.STANDARD)]), Decimal("100"), DAY)
        create_expense(
            company, receipt_date=DAY, paid_on=DAY, supplier="X", gross_amount=Decimal("11.90"), vat_rate=Decimal("19")
        )
        rows = bookings(company, START, END)
        assert {r.counter_account for r in rows if r.account == "1200"} == {"8195"}
        assert all(r.bu_key == "" for r in rows)


class TestRefusals:
    def test_without_consultant_and_client_number(self, shop):
        with pytest.raises(DatevError, match="Berater"):
            export(shop.company, START, END)

    @pytest.mark.parametrize(
        ("start", "end"), [(dt.date(2025, 12, 1), dt.date(2026, 1, 31)), (dt.date(2026, 5, 1), dt.date(2026, 4, 1))]
    )
    def test_period_must_be_inside_one_year_and_ordered(self, ledger, start, end):
        with pytest.raises(DatevError):
            bookings(ledger.company, start, end)

    @pytest.mark.parametrize("account", ["12a0", "12000", "x", "١٢٠٠", "1e3x", "-120"])
    def test_broken_stored_account_stops_the_export(self, ledger, account):
        AccountAssignment.objects.create(company=ledger.company, key="bank", account=account)
        with pytest.raises(DatevError, match="Buchhaltung"):
            bookings(ledger.company, START, END)

    def test_texts_cannot_start_like_a_formula_and_document_fields_are_cleaned(self, ledger):
        customer = create_customer(ledger.company, name='=HYPERLINK("http://evil.example";"x")', **ADDRESS)
        record_payment(invoice(ledger.company, customer, [("100", TaxTreatment.STANDARD)]), Decimal("119"), DAY)
        (row,) = bookings(ledger.company, START, END)
        assert not row.text.startswith(("=", "+", "-", "@")) and len(row.text) <= 60
        assert all(c.isalnum() or c in "$&%*+-/" for c in row.document_field)


class TestViews:
    @pytest.fixture
    def admin(self, client, ledger):
        ledger.user.is_staff = True
        ledger.user.save()
        client.force_login(ledger.user)
        return client

    def test_settings_validate_every_account(self, admin, ledger):
        data = {"chart": "skr03", "account_length": "4", "consultant_number": "12345", "client_number": "1"}
        response = admin.post("/einstellungen/buchhaltung/", {**data, "account-bank": "12x0"})
        assert "nur Ziffern" in response.content.decode()
        assert not AccountAssignment.objects.exists()
        admin.post("/einstellungen/buchhaltung/", {**data, "chart": "skr04", "account-bank": "1810"})
        assert AccountAssignment.objects.get().account == "1810"
        assert LedgerSettings.objects.get().chart == "skr04"

    @pytest.mark.parametrize(
        ("field", "value"),
        [("consultant_number", "1000"), ("client_number", "100000"), ("account_length", "9"), ("chart", "skr99"),
         ("consultant_number", "1e4"), ("client_number", "-1")],
    )  # fmt: skip
    def test_settings_refuse_out_of_range_identifiers(self, admin, ledger, field, value):
        data = {"chart": "skr03", "account_length": "4", "consultant_number": "12345", "client_number": "1"}
        admin.post("/einstellungen/buchhaltung/", {**data, field: value})
        settings = LedgerSettings.objects.get()
        assert (settings.chart, settings.account_length, settings.consultant_number, settings.client_number) == (
            "skr03", 4, 12345, 678,
        )  # fmt: skip

    def test_download_and_preview(self, client, ledger):
        client.force_login(ledger.user)
        record_payment(invoice(ledger.company, ledger.org, [("100", TaxTreatment.STANDARD)]), Decimal("119"), DAY)
        page = client.get("/auswertungen/datev/", {"von": "2026-01-01", "bis": "2026-12-31"}).content.decode()
        assert "8400" in page and "119,00" in page
        response = client.get("/auswertungen/datev/", {"von": "2026-01-01", "bis": "2026-12-31", "download": "1"})
        assert response["Content-Disposition"] == 'attachment; filename="EXTF_Buchungsstapel_20260101_20261231.csv"'
        assert response.content.startswith(b'"EXTF";700;21')

    def test_garbage_dates_fall_back_and_never_crash(self, client, ledger):
        client.force_login(ledger.user)
        for von, bis in [("2026-02-30", "x"), ("<script>", "9999-99-99"), ("", ""), ("0001-01-01", "9999-12-31")]:
            assert client.get("/auswertungen/datev/", {"von": von, "bis": bis, "download": "1"}).status_code < 500

    def test_admins_only_for_settings_and_no_export_for_drafters(self, client, ledger, django_user_model):
        user = django_user_model.objects.create_user("mitarbeit")
        client.force_login(user)
        assert client.get("/einstellungen/buchhaltung/").status_code == 403
        user.groups.add(Group.objects.get_or_create(name=DRAFTS_ONLY_GROUP)[0])
        client.force_login(user)
        assert client.get("/auswertungen/datev/").status_code == 403
