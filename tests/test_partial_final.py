"""Abschlags- und Schlussrechnungen (§ 14 Abs. 5 UStG, EN 16931 BT-113/BT-115), inkl. Storno."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from django.db import DatabaseError, transaction
from lxml import etree

from skontio.archive.services import load
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document, PrepaymentDeduction, Rendition
from skontio.documents.renditions import render_document_html
from skontio.documents.services import (
    DocumentError,
    add_deduction,
    add_line,
    amount_due,
    cancel_invoice,
    confirm_ai_review,
    create_draft,
    create_final_invoice,
    create_partial_invoice,
    finalize,
    prepaid_total,
    remove_deduction,
)
from skontio.payments.services import open_amount, payment_status, record_payment
from skontio.tax.engine import Regime, TaxTreatment
from skontio.taxstatus.services import start_regime

NS = {"ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"}
DAY = dt.date(2026, 10, 1)


@pytest.fixture
def company(db):
    company = Company.objects.create(
        name="Muster Bau",
        street="Musterweg 1",
        postcode="12345",
        city="Musterstadt",
        tax_number="123/456/78901",
        vat_id="DE123456789",
        email="rechnung@example.org",
        phone="+49 30 1234567",
        contact_name="Max Muster",
        iban="DE02120300000000202051",
    )
    start_regime(company, Regime.STANDARD, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def customer(company):
    return create_customer(
        company,
        name="Kunde GmbH",
        street="Kundenstraße 5",
        postcode="54321",
        city="Köln",
        email="einkauf@kunde.example",
        buyer_reference="Projekt 4711",
    )


@pytest.fixture
def quote(company, customer):
    """10.000 € zu 19 % plus 1.000 € zu 7 %."""
    doc = create_draft(company, Document.Type.QUOTE, customer, issue_date=DAY)
    add_line(doc, text="Montage", quantity=D("100"), unit_code="HUR", unit_price=D("100"))
    add_line(
        doc, text="Fachbuch", quantity=D("10"), unit_code="C62", unit_price=D("100"), tax_treatment=TaxTreatment.REDUCED
    )
    return finalize(doc)


def partial(quote, **kwargs) -> Document:
    kwargs.setdefault("percent", D("30"))
    return finalize(create_partial_invoice(quote, issue_date=DAY, service_date=DAY, **kwargs))


def xml_of(document: Document) -> etree._Element:
    return etree.fromstring(load(document.renditions.get(kind=Rendition.Kind.XRECHNUNG_XML).blob))


class TestPartialInvoice:
    def test_share_is_split_per_tax_treatment(self, quote):
        doc = partial(quote)
        assert doc.number == "RE-2026-0001"  # shares the invoice number range
        assert [(line.unit_price, line.tax_rate) for line in doc.lines.all()] == [
            (D("300.00"), D("7.00")),
            (D("3000.00"), D("19.00")),
        ]
        assert (doc.net_total, doc.tax_total, doc.gross_total) == (D("3300.00"), D("591.00"), D("3891.00"))

    def test_fixed_net_amount_adds_up_exactly(self, quote):
        doc = partial(quote, percent=None, net_amount=D("1000.01"))
        assert doc.net_total == D("1000.01")

    @pytest.mark.parametrize(
        ("percent", "net"), [(D("0"), None), (D("101"), None), (None, D("11000.01")), (None, None)]
    )
    def test_invalid_shares(self, quote, percent, net):
        with pytest.raises(DocumentError):
            create_partial_invoice(quote, issue_date=DAY, service_date=DAY, percent=percent, net_amount=net)

    def test_xml_type_code_326_and_reference(self, quote):
        root = xml_of(partial(quote))
        assert root.xpath("string(//*[local-name()='ExchangedDocument']/ram:TypeCode)", namespaces=NS) == "326"
        assert "Angebot AN-2026-0001" in etree.tostring(root, encoding="unicode")

    def test_payments_on_partial_invoice(self, quote):
        doc = partial(quote)
        record_payment(doc, D("3891.00"), DAY)
        assert payment_status(doc) == "paid"


class TestFinalInvoice:
    def test_deducts_partials_with_tax_per_rate(self, quote):
        first, second = partial(quote), partial(quote, percent=D("20"))
        final = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        assert final.doc_type == Document.Type.FINAL_INVOICE and final.number == "RE-2026-0003"
        assert (final.net_total, final.tax_total, final.gross_total) == (D("11000.00"), D("1970.00"), D("12970.00"))
        assert prepaid_total(final) == first.gross_total + second.gross_total == D("6485.00")
        assert amount_due(final) == D("6485.00")
        deducted = {(d.source.number, d.tax_rate): (d.net_amount, d.tax_amount) for d in final.deductions.all()}
        assert deducted[(first.number, D("19.00"))] == (D("3000.00"), D("570.00"))
        assert deducted[(first.number, D("7.00"))] == (D("300.00"), D("21.00"))

    def test_xml_carries_prepaid_and_due_amount(self, quote):
        partial(quote)
        final = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        root = xml_of(final)

        def amount(name):
            return root.xpath(
                f"string(//ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:{name})", namespaces=NS
            )

        assert (amount("GrandTotalAmount"), amount("TotalPrepaidAmount"), amount("DuePayableAmount")) == (
            "12970.00",
            "3891.00",
            "9079.00",
        )
        text = etree.tostring(root, encoding="unicode")
        assert "Abzüglich Abschlagsrechnung RE-2026-0001" in text and "Umsatzsteuer 19 % 570.00 EUR" in text

    def test_pdf_shows_deductions_and_remaining_amount(self, quote):
        partial(quote)
        final = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        html = render_document_html(final)
        assert "Schlussrechnung" in html and "abzüglich Abschlagsrechnung RE-2026-0001" in html
        assert "Noch zu zahlen" in html and "9.079,00 €" in html
        assert "Bitte überweisen Sie 9.079,00 €" in html

    def test_open_amount_and_payments_use_remaining_amount(self, quote):
        partial(quote)
        final = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        assert open_amount(final) == D("9079.00")
        record_payment(final, D("9079.00"), DAY)
        assert payment_status(final) == "paid"

    def test_without_partial_invoices_no_final_invoice(self, quote):
        with pytest.raises(DocumentError, match="keine offene Abschlagsrechnung"):
            create_final_invoice(quote, issue_date=DAY, service_date=DAY)

    def test_partial_can_be_deducted_only_once(self, quote):
        first = partial(quote)
        finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        draft = create_draft(quote.company, Document.Type.FINAL_INVOICE, quote.customer, issue_date=DAY)
        with pytest.raises(DocumentError, match="bereits in RE-2026-0002 abgezogen"):
            add_deduction(draft, first)

    def test_competing_drafts_are_checked_again_when_finalizing(self, quote):
        partial(quote)
        a = create_final_invoice(quote, issue_date=DAY, service_date=DAY)
        b = create_final_invoice(quote, issue_date=DAY, service_date=DAY)
        finalize(a)
        with pytest.raises(DocumentError, match="bereits in"):
            finalize(b)

    def test_other_customers_partial_is_rejected(self, quote, company):
        other = create_customer(company, name="Fremd", street="S", postcode="1", city="O")
        draft = create_draft(company, Document.Type.FINAL_INVOICE, other, issue_date=DAY)
        with pytest.raises(DocumentError, match="anderen Kunden"):
            add_deduction(draft, partial(quote))

    def test_deductions_must_not_exceed_total(self, quote, company):
        first = partial(quote, percent=D("100"))
        draft = create_draft(company, Document.Type.FINAL_INVOICE, quote.customer, issue_date=DAY, service_date=DAY)
        add_line(draft, text="Kleinrest", quantity=D("1"), unit_code="LS", unit_price=D("10"))
        add_deduction(draft, first)
        with pytest.raises(DocumentError, match="übersteigen"):
            finalize(draft)
        remove_deduction(draft, first)
        assert not draft.deductions.exists()

    def test_deductions_are_locked_after_finalizing(self, quote):
        partial(quote)
        final = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        deduction = final.deductions.first()
        with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
            PrepaymentDeduction.objects.filter(pk=deduction.pk).update(net_amount=D("1"))
        with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
            PrepaymentDeduction.objects.filter(pk=deduction.pk).delete()


class TestCancellation:
    def test_deducted_partial_cannot_be_cancelled_first(self, quote):
        first = partial(quote)
        finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        with pytest.raises(DocumentError, match="zuerst die Schlussrechnung stornieren"):
            cancel_invoice(first, issue_date=DAY, reason="Fehler")

    def test_cancelling_the_final_invoice_releases_the_partials(self, quote):
        first = partial(quote)
        final = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        storno = cancel_invoice(final, issue_date=DAY, reason="Falscher Leistungszeitraum")
        assert prepaid_total(storno) == D("3891.00") and amount_due(storno) == D("9079.00")
        root = xml_of(storno)
        assert root.xpath("string(//*[local-name()='ExchangedDocument']/ram:TypeCode)", namespaces=NS) == "381"
        again = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        assert again.deductions.filter(source=first).exists()
        cancel_invoice(again, issue_date=DAY, reason="x")
        cancel_invoice(first, issue_date=DAY, reason="Auftrag storniert")


class TestAiDraftsAndRevenue:
    def test_ai_final_invoice_needs_review(self, quote, django_user_model):
        partial(quote)
        final = create_final_invoice(
            quote, issue_date=DAY, service_date=DAY, origin=Document.Origin.MCP, idempotency_key="final-0001"
        )
        with pytest.raises(DocumentError, match="Prüfung"):
            finalize(final)
        confirm_ai_review(final, django_user_model.objects.create_user("x"))
        assert finalize(final).status == Document.Status.FINALIZED

    def test_revenue_counts_service_once_and_subtracts_storno(self, quote):
        from skontio.tools.registry import ToolContext, call_tool

        partial(quote)
        final = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
        ctx = ToolContext(company=quote.company, user=None, scopes=frozenset({"reports:read"}))
        assert call_tool(ctx, "revenue_summary", {"year": 2026})["invoiced_net_total"] == "11000.00"
        cancel_invoice(final, issue_date=DAY, reason="x")
        assert call_tool(ctx, "revenue_summary", {"year": 2026})["invoiced_net_total"] == "3300.00"


@pytest.mark.einvoice
@pytest.mark.django_db
def test_partial_final_and_storno_pass_official_validators(quote, tmp_path):
    from skontio.einvoice.validation import ValidatorUnavailable, validate_with_kosit, validate_with_mustang

    first = partial(quote)
    final = finalize(create_final_invoice(quote, issue_date=DAY, service_date=DAY))
    storno = cancel_invoice(final, issue_date=DAY, reason="Test")
    for document in (first, final, storno):
        pdf = load(document.renditions.get(kind=Rendition.Kind.ZUGFERD_PDF).blob)
        xml = load(document.renditions.get(kind=Rendition.Kind.XRECHNUNG_XML).blob)
        try:
            mustang = validate_with_mustang(pdf, workdir=tmp_path)
            kosit = validate_with_kosit(xml, workdir=tmp_path)
        except ValidatorUnavailable as exc:
            pytest.skip(str(exc))
        assert mustang.valid, (document.number, mustang.summary)
        assert kosit.valid, (document.number, kosit.summary)


def test_ui_flow_quote_partial_final(client, quote, django_user_model):
    client.force_login(django_user_model.objects.create_user("inhaber", is_staff=True))
    page = client.get(f"/dokumente/{quote.pk}/").content.decode()
    assert "Abschlagsrechnung erstellen" in page and "Schlussrechnung erstellen" not in page

    response = client.post(f"/dokumente/{quote.pk}/abschlag/", {"percent": "30", "service_date": "2026-10-01"})
    partial_doc = Document.objects.get(doc_type=Document.Type.PARTIAL_INVOICE)
    assert response["Location"] == f"/dokumente/{partial_doc.pk}/"
    assert client.post(f"/dokumente/{partial_doc.pk}/festschreiben/").status_code == 302

    page = client.get(f"/dokumente/{quote.pk}/").content.decode()
    assert "Offene Abschläge: RE-2026-0001" in page
    response = client.post(f"/dokumente/{quote.pk}/schlussrechnung/", {"service_date": "2026-10-01"})
    final = Document.objects.get(doc_type=Document.Type.FINAL_INVOICE)
    draft = client.get(f"/dokumente/{final.pk}/").content.decode()
    assert "RE-2026-0001 vom" in draft and "Noch zu zahlen: 9.079,00 €" in draft

    client.post(f"/dokumente/{final.pk}/abzuege/{partial_doc.pk}/entfernen/")
    assert not final.deductions.exists()
    client.post(f"/dokumente/{final.pk}/abzuege/", {"partial": partial_doc.pk})
    assert client.post(f"/dokumente/{final.pk}/festschreiben/").status_code == 302
    page = client.get(f"/dokumente/{final.pk}/").content.decode()
    assert "Noch zu zahlen" in page and "9.079,00" in page
    assert "Schlussrechnung" in client.get("/dokumente/?typ=invoice").content.decode()


def test_partial_form_needs_exactly_one_value(client, quote, django_user_model):
    client.force_login(django_user_model.objects.create_user("inhaber", is_staff=True))
    page = client.post(f"/dokumente/{quote.pk}/abschlag/", {"percent": "10", "net_amount": "100"}).content.decode()
    assert "Entweder einen Prozentsatz oder einen Nettobetrag" in page
    assert not Document.objects.filter(doc_type=Document.Type.PARTIAL_INVOICE).exists()


def test_mcp_tools_create_partial_and_final_drafts(quote, django_user_model):
    from skontio.tools.registry import ToolContext, ToolError, call_tool

    user = django_user_model.objects.create_user("inhaber", is_staff=True)
    ctx = ToolContext(company=quote.company, user=user, scopes=frozenset({"drafts:write"}))
    args = {"quote_number": quote.number, "idempotency_key": "abschlag-0001", "service_date": "2026-10-01"}
    with pytest.raises(ToolError, match="percent oder net_amount"):
        call_tool(ctx, "partial_invoice_draft_create", args)
    draft = call_tool(ctx, "partial_invoice_draft_create", {**args, "percent": "30"})
    assert draft["type"] == "partial_invoice" and draft["totals"]["gross"] == "3891.00"
    assert draft["ai_review_required"] is True
    partial_doc = Document.objects.get(pk=draft["document_id"])
    confirm_ai_review(partial_doc, user)
    finalize(partial_doc)
    final = call_tool(ctx, "final_invoice_draft_create", {**args, "idempotency_key": "schluss-0001"})
    assert final["totals"]["prepaid"] == "3891.00" and final["totals"]["due"] == "9079.00"


class TestOrderConfirmation:
    def test_quote_to_order_confirmation_to_partial_and_final(self, quote):
        from skontio.documents.services import create_order_confirmation

        confirmation = finalize(create_order_confirmation(quote, issue_date=DAY))
        assert confirmation.number == "AB-2026-0001" and confirmation.parent == quote
        assert [line.text for line in confirmation.lines.all()] == ["Montage", "Fachbuch"]
        on_quote = partial(quote, percent=D("10"))
        on_confirmation = partial(confirmation, percent=D("20"))
        assert "gemäß Auftragsbestätigung AB-2026-0001" in on_confirmation.lines.first().text
        assert "zur Auftragsbestätigung AB-2026-0001" in render_document_html(on_confirmation)
        final = finalize(create_final_invoice(confirmation, issue_date=DAY, service_date=DAY))
        assert {d.source for d in final.deductions.all()} == {on_quote, on_confirmation}

    def test_order_confirmation_only_from_quote(self, quote):
        from skontio.documents.services import create_order_confirmation

        confirmation = finalize(create_order_confirmation(quote, issue_date=DAY))
        with pytest.raises(DocumentError, match="aus einem Angebot"):
            create_order_confirmation(confirmation, issue_date=DAY)

    def test_ui_button(self, client, quote, django_user_model):
        client.force_login(django_user_model.objects.create_user("inhaber", is_staff=True))
        assert "Auftragsbestätigung erstellen" in client.get(f"/dokumente/{quote.pk}/").content.decode()
        response = client.post(f"/dokumente/{quote.pk}/auftragsbestaetigung/")
        confirmation = Document.objects.get(doc_type=Document.Type.ORDER_CONFIRMATION)
        assert response["Location"] == f"/dokumente/{confirmation.pk}/"
