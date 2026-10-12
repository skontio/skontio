"""Steuerstatus-Wechsel und Korrektur-Assistent: rückwirkend überschrittene Kleinunternehmergrenze."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.audit.models import AuditEvent
from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import (
    DocumentError,
    add_line_from_item,
    create_draft,
    create_final_invoice,
    create_partial_invoice,
    finalize,
    preview_totals,
)
from skontio.payments.services import open_amount, paid_amount, record_payment
from skontio.tax.engine import Regime
from skontio.taxstatus.corrections import candidates, correct_invoice, transfer_payments
from skontio.taxstatus.models import TaxStatusPeriod
from skontio.taxstatus.services import change_regime, start_regime

D = Decimal
Reason = TaxStatusPeriod.Reason


@pytest.fixture
def setup(db, django_user_model):
    company = Company.objects.create(
        name="Muster Selbstständig",
        street="Weg 1",
        postcode="12345",
        city="Stadt",
        tax_number="123/456/78901",
        iban="DE02120300000000202051",
        email="rechnung@example.org",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    customer = create_customer(company, name="Kunde GmbH", street="W 1", postcode="2", city="K")
    item = create_item(company, name="Projekt", unit="LS", net_price=D("1"))
    user = django_user_model.objects.create_user("inhaber", is_staff=True)
    return company, customer, item, user


def invoice(setup, amount, service, paid_on=None):
    company, customer, item, _ = setup
    doc = create_draft(company, Document.Type.INVOICE, customer, issue_date=service, service_date=service)
    add_line_from_item(doc, item, quantity=D("1"))
    line = doc.lines.get()
    line.unit_price = D(amount)
    line.save()
    doc = finalize(doc)
    if paid_on:
        record_payment(doc, doc.gross_total, paid_on)
    return doc


@pytest.fixture
def year(setup):
    """50k in March, 60k in June (crosses 100k on 15 June), 1k in July – all as Kleinunternehmer."""
    first = invoice(setup, "50000", dt.date(2026, 3, 1), paid_on=dt.date(2026, 3, 10))
    crossing = invoice(setup, "60000", dt.date(2026, 6, 1), paid_on=dt.date(2026, 6, 15))
    later = invoice(setup, "1000", dt.date(2026, 7, 1))
    return first, crossing, later


def switch(setup, on=dt.date(2026, 6, 1)):
    company, *_, user = setup
    change_regime(company, Regime.STANDARD, on, Reason.THRESHOLD_CURRENT_YEAR, confirmed_by=user)


class TestCandidates:
    def test_nothing_before_the_switch_is_confirmed(self, setup, year):
        assert candidates(setup[0]) == []

    def test_invoices_after_the_switch_date(self, setup, year):
        first, crossing, later = year
        switch(setup)
        found = candidates(setup[0])
        assert [c.document for c in found] == [crossing, later]
        assert found[0].recorded is Regime.SMALL_BUSINESS and found[0].current is Regime.STANDARD
        assert found[0].paid == D("60000.00") and found[1].paid == 0


class TestCorrection:
    def test_keep_gross_takes_vat_out_of_the_old_amount(self, setup, year):
        *_, later = year
        switch(setup)
        draft = correct_invoice(later, keep="gross", issue_date=dt.date(2026, 10, 12))
        assert later.children.filter(doc_type=Document.Type.CANCELLATION).exists()
        assert draft.replaces == later and draft.status == Document.Status.DRAFT
        assert draft.intro_text.startswith(f"Diese Rechnung ersetzt die Rechnung {later.number} vom 01.07.2026")
        totals = preview_totals(draft)
        assert totals.grand_total == D("1000.00")
        assert totals.tax_total == D("159.66")
        assert later not in [c.document for c in candidates(setup[0])]  # cancelled ones are not listed

    def test_keep_net_adds_vat(self, setup, year):
        _, crossing, _ = year
        switch(setup)
        draft = correct_invoice(crossing, keep="net", issue_date=dt.date(2026, 10, 12))
        assert preview_totals(draft).grand_total == D("71400.00")

    def test_replacement_is_finalized_with_vat_and_payments_move(self, setup, year):
        _, crossing, _ = year
        switch(setup)
        draft = correct_invoice(crossing, keep="net", issue_date=dt.date(2026, 10, 12))
        replacement = finalize(draft)
        assert replacement.regime == Regime.STANDARD and replacement.tax_total == D("11400.00")
        moved, left = transfer_payments(replacement)
        assert (moved, left) == (D("60000.00"), D("0"))
        assert paid_amount(crossing) == 0 and paid_amount(replacement) == D("60000.00")
        assert open_amount(replacement) == D("11400.00")
        reversal = crossing.payments.get(amount__lt=0)
        assert reversal.received_on == dt.date(2026, 6, 15)  # same day: the threshold history stays right
        assert transfer_payments(replacement) == (D("0"), D("0"))
        assert AuditEvent.objects.filter(action="payments.transfer").count() == 2

    def test_payment_larger_than_open_amount_stays(self, setup, year):
        _, crossing, _ = year
        switch(setup)
        replacement = finalize(correct_invoice(crossing, keep="net", issue_date=dt.date(2026, 10, 12)))
        record_payment(replacement, D("20000"), dt.date(2026, 10, 12))
        moved, left = transfer_payments(replacement)
        assert (moved, left) == (D("0"), D("60000.00"))
        assert paid_amount(crossing) == D("60000.00")

    def test_payments_only_after_finalizing(self, setup, year):
        _, crossing, _ = year
        switch(setup)
        draft = correct_invoice(crossing, keep="net", issue_date=dt.date(2026, 10, 12))
        with pytest.raises(DocumentError, match="festgeschrieben"):
            transfer_payments(draft)

    def test_matching_invoice_is_refused(self, setup, year):
        first, *_ = year
        switch(setup)
        with pytest.raises(DocumentError, match="passt bereits"):
            correct_invoice(first, keep="net", issue_date=dt.date(2026, 10, 12))

    def test_final_invoices_are_left_to_the_user(self, setup):
        company, customer, item, _ = setup
        quote = create_draft(company, Document.Type.QUOTE, customer, issue_date=dt.date(2026, 6, 1))
        add_line_from_item(quote, item, quantity=D("100"))
        quote = finalize(quote)
        partial = create_partial_invoice(
            quote, issue_date=dt.date(2026, 6, 2), service_date=dt.date(2026, 6, 2), percent=D("50")
        )
        finalize(partial)
        final = finalize(
            create_final_invoice(quote, issue_date=dt.date(2026, 6, 20), service_date=dt.date(2026, 6, 20))
        )
        switch(setup)
        assert {c.document.doc_type for c in candidates(company)} == {"partial_invoice", "final_invoice"}
        with pytest.raises(DocumentError, match="von Hand"):
            correct_invoice(final, keep="net", issue_date=dt.date(2026, 10, 12))

    def test_correction_is_audited(self, setup, year):
        *_, later = year
        switch(setup)
        correct_invoice(later, keep="gross", issue_date=dt.date(2026, 10, 12))
        event = AuditEvent.objects.get(action="document.correction")
        assert event.data["keep"] == "gross" and event.data["to"] == "standard"


class TestEnglish:
    def test_texts_follow_the_invoice_language(self, setup):
        company, customer, item, _ = setup
        customer.language = "en"
        customer.save()
        doc = invoice(setup, "500", dt.date(2026, 7, 1))
        switch(setup)
        draft = correct_invoice(doc, keep="net", issue_date=dt.date(2026, 10, 12))
        storno = doc.children.get(doc_type=Document.Type.CANCELLATION)
        assert draft.intro_text.startswith(f"This invoice replaces invoice {doc.number} of 1 Jul 2026")
        assert storno.cancellation_reason.startswith("Invoice correction due to a change in VAT status")


class TestWeb:
    def test_page_suggests_the_crossing_date(self, client, setup, year):
        client.force_login(setup[3])
        page = client.get("/einstellungen/steuerstatus/").content.decode()
        assert "Am 15.06.2026 überschritten" in page
        assert 'value="2026-06-15"' in page and "Wechsel zu: Regelbesteuerung" in page
        assert "Alle festgeschriebenen Rechnungen passen" in page

    def test_change_needs_confirmation(self, client, setup, year):
        client.force_login(setup[3])
        data = {"valid_from": "2026-06-01", "reason": Reason.THRESHOLD_CURRENT_YEAR}
        assert client.post("/einstellungen/steuerstatus/wechsel/", data).status_code == 400
        response = client.post("/einstellungen/steuerstatus/wechsel/", data | {"confirm": "on"})
        assert response.status_code == 302
        assert TaxStatusPeriod.objects.filter(regime=Regime.STANDARD, valid_from=dt.date(2026, 6, 1)).exists()
        assert AuditEvent.objects.filter(action="taxstatus.change").exists()

    def test_correct_and_transfer_through_the_ui(self, client, setup, year):
        _, crossing, _ = year
        switch(setup)
        client.force_login(setup[3])
        page = client.get("/einstellungen/steuerstatus/").content.decode()
        assert crossing.number in page and "Korrigieren" in page
        response = client.post(f"/einstellungen/steuerstatus/korrektur/{crossing.pk}/", {"keep": "net"})
        draft = Document.objects.get(replaces=crossing)
        assert response["Location"] == f"/dokumente/{draft.pk}/"
        replacement = finalize(draft)
        page = client.get(f"/dokumente/{replacement.pk}/").content.decode()
        assert "Zahlungen übernehmen" in page and "60.000,00 €" in page
        assert "Ersetzt durch" in client.get(f"/dokumente/{crossing.pk}/").content.decode()
        client.post(f"/dokumente/{replacement.pk}/zahlungen-uebernehmen/")
        assert paid_amount(replacement) == D("60000.00")

    def test_dashboard_links_to_the_assistant(self, client, setup, year):
        client.force_login(setup[3])
        assert 'href="/einstellungen/steuerstatus/"' in client.get("/").content.decode()

    def test_only_admins(self, client, setup, django_user_model):
        client.force_login(django_user_model.objects.create_user("mitarbeiter"))
        assert client.get("/einstellungen/steuerstatus/").status_code == 403
