"""Belegerkennung: die KI schlägt vor, Skontio prüft jeden Wert, gespeichert wird erst nach der Prüfung."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from skontio.assistant.providers import Fake, ProviderError, Turn
from skontio.expenses import recognition
from skontio.expenses.models import Expense
from skontio.expenses.services import ExpenseError, add_pending
from skontio.rendering.pdf import render_pdf_a3
from skontio.web import expense_views

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 5000
ANSWER = {
    "supplier": "Bürobedarf Müller GmbH", "supplier_country": "de", "receipt_date": "2026-10-14",
    "invoice_number": "R-778", "gross_amount": "119,00", "net_amount": "100.00", "vat_amount": "19.00",
    "vat_rate": "19", "description": "Druckerpapier und Toner", "category": "office",
}  # fmt: skip


def fake(answer) -> Fake:
    return Fake([Turn(text=answer if isinstance(answer, str) else json.dumps(answer))])


def test_photo_is_read_and_cleaned(shop):
    pending = add_pending(shop.company, JPEG, filename="bon.jpg")
    provider = fake("Hier das Ergebnis:\n```json\n" + json.dumps(ANSWER) + "\n```")
    result = recognition.recognize(pending, provider=provider)
    assert provider.requests[0]["attachment"] == "image/jpeg"
    assert result == {
        "warnings": [], "supplier": "Bürobedarf Müller GmbH", "supplier_country": "DE", "receipt_date": "2026-10-14",
        "invoice_number": "R-778", "gross_amount": "119.00", "net_amount": "100.00", "vat_amount": "19.00",
        "vat_rate": "19", "description": "Druckerpapier und Toner", "category": "office",
    }  # fmt: skip
    pending.refresh_from_db()
    assert pending.recognized == result and pending.expense is None  # nothing saved as an expense


def test_pdf_with_text_is_sent_as_text_to_any_model(shop):
    pdf = render_pdf_a3("<p>Rechnung R-1 Hosting AG Gesamt 11,90 EUR</p>", title="R-1")
    pending = add_pending(shop.company, pdf, filename="r.pdf")
    provider = fake({"supplier": "Hosting AG", "gross_amount": 11.9})
    result = recognition.recognize(pending, provider=provider)
    assert provider.requests[0]["attachment"] is None and "Hosting AG Gesamt 11,90" in provider.requests[0]["text"]
    assert result["gross_amount"] == "11.90"


def test_scans_missing_provider_and_errors(shop, monkeypatch):
    monkeypatch.setattr(recognition, "_text_of_pdf", lambda data: "")
    scan = add_pending(shop.company, b"%PDF-1.4\n%%EOF", filename="scan.pdf")
    with pytest.raises(ExpenseError, match="keinen Text"):
        recognition.recognize(scan, provider=fake({}))
    with pytest.raises(ExpenseError, match="kein KI-Anbieter"):
        recognition.recognize(scan, provider=None)
    photo = add_pending(shop.company, JPEG, filename="b.jpg")
    with pytest.raises(ExpenseError, match="keine verwertbare"):
        recognition.recognize(photo, provider=fake("Ich kann das nicht lesen."))

    class Down(Fake):
        def read(self, *args, **kwargs):
            raise ProviderError("nicht erreichbar")

    with pytest.raises(ExpenseError, match="nicht erreichbar"):
        recognition.recognize(photo, provider=Down([]))


def test_implausible_values_are_dropped():
    result = recognition.clean({
        "supplier": {"x": 1}, "supplier_country": "Deutschland", "receipt_date": "1999-12-31",
        "gross_amount": "-5", "net_amount": "NaN", "vat_amount": "1e99", "vat_rate": "77", "category": "kaviar",
        "description": "<script>alert(1)</script>",
    })  # fmt: skip
    assert result == {"warnings": [], "description": "<script>alert(1)</script>"}  # text is escaped when shown
    mismatch = recognition.clean({"gross_amount": "100", "net_amount": "50", "vat_amount": "9.50"})
    assert mismatch["warnings"]


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(st.sampled_from([*ANSWER, "x"]), st.one_of(st.none(), st.text(), st.integers(),
       st.floats(allow_nan=True), st.booleans(), st.lists(st.integers()))))  # fmt: skip
def test_clean_never_crashes_and_stays_in_range(data):
    result = recognition.clean(data)
    for key in ("gross_amount", "net_amount", "vat_amount"):
        if key in result:
            assert Decimal("0") <= Decimal(result[key]) < Decimal("1e9")
    if "vat_rate" in result:
        assert Decimal("0") <= Decimal(result["vat_rate"]) <= 30
    if "category" in result:
        assert result["category"] in Expense.Category.values
    json.dumps(result)


def test_form_is_prefilled_and_saving_stays_manual(client, shop, monkeypatch):
    pending = add_pending(shop.company, JPEG, filename="bon.jpg")
    monkeypatch.setattr(expense_views, "get_provider", lambda: fake(ANSWER))
    monkeypatch.setattr(expense_views, "_ai_available", lambda: True)
    client.force_login(shop.user)
    assert "Beleg mit KI auslesen" in client.get(f"/ausgaben/neu/?beleg={pending.pk}").content.decode()
    response = client.post(f"/ausgaben/belege/{pending.pk}/auslesen/")
    assert response.status_code == 302 and Expense.objects.count() == 0
    page = client.get(f"/ausgaben/neu/?beleg={pending.pk}").content.decode()
    assert "Bürobedarf Müller GmbH" in page and "Rechnung R-778: Druckerpapier und Toner" in page
    assert "von der KI vorgeschlagen" in page
    initial = recognition.initial_from(pending.__class__.objects.get(pk=pending.pk).recognized)
    assert initial["receipt_date"] == dt.date(2026, 10, 14) and initial["gross_amount"] == Decimal("119.00")


def test_other_companies_receipts_are_not_reachable(client, shop, monkeypatch):
    pending = add_pending(shop.company, JPEG, filename="bon.jpg")
    monkeypatch.setattr(expense_views, "get_provider", lambda: fake(ANSWER))
    client.force_login(shop.user)
    assert client.post(f"/ausgaben/belege/{pending.pk + 999}/auslesen/").status_code == 404
