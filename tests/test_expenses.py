"""Ausgaben mit Belegen und Einnahmenüberschussrechnung (Zufluss/Abfluss, Kleinunternehmer und Wechsel)."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from skontio.audit.models import AuditEvent
from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.expenses.models import Expense
from skontio.expenses.services import (
    ExpenseError,
    cancel_expense,
    create_expense,
    hints,
    input_vat,
    net_amount,
    reverse_charge_vat,
    update_expense,
)
from skontio.payments.services import record_payment
from skontio.reports.euer import euer, journal
from skontio.tax.engine import Regime
from skontio.taxstatus.models import TaxStatusPeriod
from skontio.taxstatus.services import change_regime, start_regime
from skontio.tools.registry import call_tool

D = Decimal
TODAY = timezone.localdate()
YEAR = TODAY.year


def jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def small(db, django_user_model):
    company = Company.objects.create(
        name="Nebengewerbe",
        street="Weg 1",
        postcode="1",
        city="S",
        tax_number="1/2/3",
        iban="DE02120300000000202051",
        email="e@example.org",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(YEAR - 1, 1, 1))
    user = django_user_model.objects.create_user("inhaber", is_staff=True)
    return company, user


def expense(company, **kwargs):
    fields = {
        "receipt_date": dt.date(YEAR, 1, 10),
        "paid_on": dt.date(YEAR, 1, 10),
        "supplier": "Hosting GmbH",
        "gross_amount": D("119.00"),
        "vat_rate": D("19"),
        "category": Expense.Category.SOFTWARE,
    } | kwargs
    return create_expense(company, **fields)


def paid_invoice(company, amount, day):
    customer = create_customer(company, name="Kunde", street="W 1", postcode="2", city="K")
    doc = create_draft(company, Document.Type.INVOICE, customer, issue_date=day, service_date=day)
    add_line_from_item(doc, create_item(company, name="Leistung", unit="LS", net_price=D(amount)), quantity=D("1"))
    doc = finalize(doc)
    record_payment(doc, doc.gross_total, day)
    return doc


class TestExpenses:
    def test_numbers_receipt_and_amounts(self, small):
        company, user = small
        e = expense(company, receipt=jpeg(), receipt_filename="kassenbon.jpg")
        assert e.number == f"AU-{YEAR}-0001" and e.receipt.mime == "image/jpeg"
        assert e.receipt.retention_until == dt.date(YEAR + 8, 12, 31)
        assert (net_amount(e), input_vat(e)) == (D("100.00"), D("19.00"))
        assert expense(company).number == f"AU-{YEAR}-0002"

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"supplier": " "}, "Lieferanten"),
            ({"gross_amount": D("0")}, "größer als 0"),
            ({"supplier_country": "Irland"}, "zweistelligen"),
            ({"vat_rate": D("40")}, "USt-Satz"),
            ({"category": "yacht"}, "Kategorie"),
            ({"paid_on": TODAY + dt.timedelta(days=5)}, "Zukunft"),
        ],
    )
    def test_validation(self, small, kwargs, message):
        with pytest.raises(ExpenseError, match=message):
            expense(small[0], **kwargs)

    def test_receipt_types(self, small):
        with pytest.raises(ExpenseError):
            expense(small[0], receipt=b"MZ\x90\x00", receipt_filename="virus.pdf")
        with pytest.raises(ExpenseError, match="PDF, Foto"):
            expense(small[0], receipt=b"Notiz", receipt_filename="notiz.txt")

    def test_changes_are_audited_and_cancel_replaces_delete(self, small):
        company, user = small
        e = expense(company, user=user)
        update_expense(e, user=user, gross_amount=D("238.00"), supplier="Hosting GmbH")
        event = AuditEvent.objects.get(action="expense.update")
        assert event.data["changed"] == {"gross_amount": ["119.00", "238.00"]}
        cancel_expense(e, reason="doppelt", user=user)
        with pytest.raises(ExpenseError, match="storniert"):
            update_expense(e, note="x")
        with pytest.raises(ExpenseError, match="Grund"):
            cancel_expense(expense(company), reason=" ")

    def test_hints(self, small):
        company, _ = small
        ads = expense(
            company, supplier="Ads Ireland Ltd", supplier_country="IE", reverse_charge=True, gross_amount=D("100")
        )
        assert reverse_charge_vat(ads) == D("19.00") and input_vat(ads) == 0
        text = " ".join(hints(ads, Regime.SMALL_BUSINESS))
        assert "Auch als Kleinunternehmer schuldest du hierfür 19,00 €" in text and "Beleg fehlt" in text
        foreign = expense(company, supplier="Tool Inc", supplier_country="US", vat_rate=D("0"))
        assert "§ 13b" in " ".join(hints(foreign, Regime.SMALL_BUSINESS))
        laptop = expense(company, category=Expense.Category.GWG, gross_amount=D("1190"))
        assert "Anlagegut" in " ".join(hints(laptop, Regime.SMALL_BUSINESS))


class TestEuer:
    def test_small_business_counts_gross_by_payment_date(self, small):
        company, _ = small
        paid_invoice(company, "1000", dt.date(YEAR, 2, 1))
        expense(company, gross_amount=D("119"))  # software, paid in January
        expense(company, category=Expense.Category.HOSPITALITY, gross_amount=D("100"))
        expense(company, paid_on=None, gross_amount=D("50"))  # unpaid: not yet an expense
        expense(company, receipt_date=dt.date(YEAR - 1, 12, 20), paid_on=dt.date(YEAR - 1, 12, 28))  # last year
        cancelled = expense(company, gross_amount=D("999"))
        cancel_expense(cancelled, reason="Fehler")
        report = euer(company, YEAR)
        assert [(line.key, line.amount) for line in report.income] == [("small_business", D("1000.00"))]
        assert {line.key: line.amount for line in report.expenses} == {
            "software": D("119.00"),  # gross: no input tax deduction
            "hospitality": D("70.00"),
        }
        assert report.profit == D("811.00")
        assert report.non_deductible == D("30.00") and report.unpaid_expenses == 1
        assert any("ohne Zahlungsdatum" in note for note in report.notes)

    def test_change_to_standard_splits_the_year(self, small):
        company, user = small
        paid_invoice(company, "1000", dt.date(YEAR, 2, 1))  # Kleinunternehmer
        change_regime(
            company,
            Regime.STANDARD,
            dt.date(YEAR, 6, 1),
            TaxStatusPeriod.Reason.THRESHOLD_CURRENT_YEAR,
            confirmed_by=user,
        )
        paid_invoice(company, "2000", dt.date(YEAR, 7, 1))  # 2000 + 380 VAT
        expense(company, gross_amount=D("119"))  # January: gross
        expense(company, receipt_date=dt.date(YEAR, 7, 2), paid_on=dt.date(YEAR, 7, 2), gross_amount=D("238"))
        report = euer(company, YEAR)
        income = {line.key: line.amount for line in report.income}
        assert income == {"small_business": D("1000.00"), "taxable": D("2000.00"), "collected_vat": D("380.00")}
        expenses = {line.key: line.amount for line in report.expenses}
        assert expenses == {"software": D("319.00"), "input_vat": D("38.00")}  # 119 gross + 200 net
        assert report.profit == D("3380.00") - D("357.00")

    def test_reverse_charge_is_reported(self, small):
        company, _ = small
        expense(company, supplier="Ads Ireland", supplier_country="IE", reverse_charge=True, gross_amount=D("200"))
        report = euer(company, YEAR)
        assert report.reverse_charge_vat == D("38.00")
        assert any("§ 13b" in note for note in report.notes)

    def test_journal(self, small):
        company, _ = small
        paid_invoice(company, "500", dt.date(YEAR, 3, 1))
        expense(company)
        entries = journal(company, YEAR)
        assert [e.kind for e in entries] == ["Ausgabe", "Einnahme"]


class TestWeb:
    def test_capture_with_photo_and_view(self, client, small):
        company, user = small
        client.force_login(user)
        response = client.post(
            "/ausgaben/neu/",
            {
                "receipt": SimpleUploadedFile("bon.jpg", jpeg(), content_type="image/jpeg"),
                "receipt_date": f"{YEAR}-01-10",
                "supplier": "Bürobedarf",
                "gross_amount": "23,80",
                "vat_rate": "19",
                "category": "office",
                "paid_on": f"{YEAR}-01-10",
                "method": "card",
                "supplier_country": "de",
            },
        )
        e = Expense.objects.get()
        assert response["Location"] == f"/ausgaben/{e.pk}/"
        assert (e.gross_amount, e.supplier_country, e.receipt.mime) == (D("23.80"), "DE", "image/jpeg")
        page = client.get(f"/ausgaben/{e.pk}/").content.decode()
        assert "bon.jpg" in page and "expense.create" in page
        receipt = client.get(f"/ausgaben/{e.pk}/beleg/")
        assert "sandbox" in receipt["Content-Security-Policy"]
        listing = client.get(f"/ausgaben/?jahr={YEAR}").content.decode()
        assert e.number in listing and "23,80" in listing

    def test_form_errors_and_cancel(self, client, small):
        company, user = small
        client.force_login(user)
        response = client.post("/ausgaben/neu/", {"receipt_date": f"{YEAR}-01-10", "supplier": "x"})
        assert response.status_code == 200 and not Expense.objects.exists()
        e = expense(company)
        client.post(f"/ausgaben/{e.pk}/stornieren/", {"reason": "doppelt"})
        e.refresh_from_db()
        assert e.cancelled_at is not None
        assert client.post(f"/ausgaben/{e.pk}/", {"supplier": "y"}).status_code == 409

    def test_euer_page_and_csv(self, client, small):
        company, user = small
        client.force_login(user)
        paid_invoice(company, "1000", dt.date(YEAR, 2, 1))
        expense(company, supplier="=HYPERLINK(1)")
        page = client.get(f"/auswertungen/euer/?jahr={YEAR}").content.decode()
        assert "Gewinn: 881,00 €" in page and "Betriebseinnahmen als Kleinunternehmer" in page
        csv = client.get(f"/auswertungen/euer.csv?jahr={YEAR}").content.decode("utf-8-sig")
        assert csv.splitlines()[0].startswith("Datum;Art;Beleg")
        assert "'=HYPERLINK(1)" in csv  # no formula injection into spreadsheets
        assert "119,00" in csv

    def test_other_company_is_hidden(self, client, small):
        company, user = small
        other = Company.objects.create(name="Fremd")
        foreign = expense(other)
        client.force_login(user)
        assert client.get(f"/ausgaben/{foreign.pk}/").status_code == 404
        assert client.get(f"/ausgaben/{foreign.pk}/beleg/").status_code == 404


def test_mcp_euer_summary(small):
    from skontio.tools.registry import ToolContext

    company, user = small
    paid_invoice(company, "1000", dt.date(YEAR, 2, 1))
    expense(company)
    ctx = ToolContext(company=company, user=user, scopes=frozenset({"reports:read"}), client_id="test")
    result = call_tool(ctx, "euer_summary", {"year": YEAR})
    assert result["profit"] == "881.00" and result["expenses"][0]["group"] == "Software, Lizenzen, Hosting"


class TestReceiptInbox:
    def test_share_target_without_csrf_token_but_logged_in(self, small):
        from django.test import Client

        company, user = small
        browser = Client(enforce_csrf_checks=True)
        shared = {"receipt": SimpleUploadedFile("IMG_1.jpg", jpeg(), content_type="image/jpeg"), "title": "Tanken"}
        anonymous = browser.post("/ausgaben/teilen/", shared)
        assert anonymous.status_code == 302 and "login" in anonymous["Location"]
        browser.force_login(user)
        shared["receipt"] = SimpleUploadedFile("IMG_1.jpg", jpeg(), content_type="image/jpeg")
        response = browser.post("/ausgaben/teilen/", shared)
        assert response["Location"] == "/ausgaben/belege/"
        from skontio.expenses.models import PendingReceipt

        pending = PendingReceipt.objects.get()
        assert pending.note == "Tanken" and pending.blob.retention_until is None

    def test_bulk_upload_capture_and_discard(self, client, small):
        from skontio.expenses.models import PendingReceipt

        company, user = small
        client.force_login(user)
        files = [SimpleUploadedFile(f"bon{i}.jpg", jpeg() + bytes([i]), content_type="image/jpeg") for i in range(2)]
        files.append(SimpleUploadedFile("virus.exe", b"MZ\x90\x00", content_type="application/pdf"))
        client.post("/ausgaben/belege/", {"files": files})
        assert PendingReceipt.objects.count() == 2
        first, second = PendingReceipt.objects.order_by("pk")
        page = client.get("/ausgaben/belege/").content.decode()
        assert "Als Ausgabe erfassen" in page and "bon0.jpg" in page
        assert "bon0.jpg" in client.get(f"/ausgaben/neu/?beleg={first.pk}").content.decode()
        client.post(
            "/ausgaben/neu/",
            {
                "beleg": first.pk,
                "receipt_date": f"{YEAR}-01-10",
                "supplier": "Tankstelle",
                "gross_amount": "50,00",
                "vat_rate": "19",
                "category": "vehicle",
                "paid_on": f"{YEAR}-01-10",
                "method": "card",
                "supplier_country": "DE",
            },
        )
        e = Expense.objects.get()
        first.refresh_from_db()
        assert first.expense == e and e.receipt == first.blob
        assert e.receipt.retention_until == dt.date(YEAR + 8, 12, 31)
        with pytest.raises(ExpenseError, match="bereits"):
            create_expense(company, pending=first, receipt_date=TODAY, supplier="x", gross_amount=D("1"))
        blob_key = second.blob.storage_key
        client.post(f"/ausgaben/belege/{second.pk}/verwerfen/")
        from skontio.archive.models import Blob

        assert not PendingReceipt.objects.filter(pk=second.pk).exists()
        assert not Blob.objects.filter(storage_key=blob_key).exists()

    def test_manifest_offers_share_target(self, client):
        data = client.get("/manifest.webmanifest").json()
        assert data["share_target"]["action"] == "/ausgaben/teilen/"
        assert data["share_target"]["params"]["files"][0]["name"] == "receipt"
