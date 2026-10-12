"""Dokumentsprache: englische Angebote, Rechnungen, E-Rechnungs-Notizen und Mailtexte."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import facturx
import pytest
from lxml import etree
from pypdf import PdfReader

from skontio.archive.services import load
from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.languages import Lang
from skontio.documents.models import Document, Rendition
from skontio.documents.renditions import render_document_html
from skontio.documents.services import (
    DocumentError,
    add_line_from_item,
    cancel_invoice,
    convert_quote_to_invoice,
    create_draft,
    create_partial_invoice,
    finalize,
    update_draft,
)
from skontio.mail.services import default_message
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime
from skontio.tools.registry import call_tool

D = Decimal


class TestFormatting:
    def test_german(self):
        t = Lang("de")
        assert (t.amount(D("1234.5")), t.money(D("-3")), t.percent(D("19.00"))) == ("1.234,50", "-3,00 €", "19 %")
        assert t.date(dt.date(2026, 3, 1)) == "01.03.2026"

    def test_english(self):
        t = Lang("en")
        assert (t.amount(D("1234.5")), t.money(D("80")), t.percent(D("7"))) == ("1,234.50", "€80.00", "7%")
        assert t.date(dt.date(2026, 3, 1)) == "1 Mar 2026"
        assert t.quantity(D("2.50")) == "2.5"

    def test_unknown_language_falls_back_to_german(self):
        assert Lang("fr").code == "de"


@pytest.fixture
def company(db):
    company = Company.objects.create(
        name="Muster Selbstständig",
        street="Musterweg 1",
        postcode="12345",
        city="Musterstadt",
        tax_number="123/456/78901",
        seller_identifier="123/456/78901",
        email="rechnung@example.org",
        phone="+49 30 1234567",
        contact_name="Max Muster",
        iban="DE02120300000000202051",
        bic="BYLADEM1001",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def client_en(company):
    return create_customer(
        company,
        name="Acme Ltd",
        street="1 High Street",
        postcode="EC1A 1BB",
        city="London",
        country="GB",
        email="ap@acme.example",
        buyer_reference="PO 77",
        language="en",
    )


@pytest.fixture
def item(company):
    return create_item(company, name="Consulting", unit="HUR", net_price=D("80"))


def english_invoice(company, customer, item, **kwargs):
    doc = create_draft(
        company,
        Document.Type.INVOICE,
        customer,
        issue_date=dt.date(2026, 10, 12),
        service_date=dt.date(2026, 10, 9),
        **kwargs,
    )
    add_line_from_item(doc, item, quantity=D("2.5"))
    return finalize(doc)


class TestDrafts:
    def test_language_comes_from_the_customer(self, company, client_en):
        doc = create_draft(company, Document.Type.QUOTE, client_en, issue_date=dt.date(2026, 10, 12))
        assert doc.language == "en"

    def test_explicit_language_wins_and_is_checked(self, company, client_en):
        doc = create_draft(company, Document.Type.QUOTE, client_en, issue_date=dt.date(2026, 10, 12), language="de")
        assert doc.language == "de"
        with pytest.raises(DocumentError, match="Dokumentsprache"):
            create_draft(company, Document.Type.QUOTE, client_en, issue_date=dt.date(2026, 10, 12), language="fr")
        with pytest.raises(DocumentError, match="Dokumentsprache"):
            update_draft(doc, language="xx")

    def test_follow_up_documents_keep_the_language(self, company, client_en, item):
        quote = create_draft(company, Document.Type.QUOTE, client_en, issue_date=dt.date(2026, 10, 1))
        add_line_from_item(quote, item, quantity=D("10"))
        quote = finalize(quote)
        client_en.language = "de"  # changing the customer later does not change existing work
        client_en.save()
        invoice = convert_quote_to_invoice(quote, issue_date=dt.date(2026, 10, 12))
        partial = create_partial_invoice(quote, issue_date=dt.date(2026, 10, 12), percent=D("30"))
        assert invoice.language == partial.language == "en"
        assert partial.lines.get().text == f"Down payment of 30% as per Quotation {quote.number}"


class TestEnglishInvoice:
    def test_tax_note_is_translated_and_frozen(self, company, client_en, item):
        invoice = english_invoice(company, client_en, item)
        assert invoice.tax_note.startswith("VAT exemption for small businesses under § 19 UStG")
        assert invoice.lines.get().tax_exemption_reason == invoice.tax_note

    def test_pdf_is_english(self, company, client_en, item):
        invoice = english_invoice(company, client_en, item)
        html = render_document_html(invoice)
        for expected in (
            'lang="en"',
            "Invoice No.",
            "Date of supply",
            "9 Oct 2026",
            "Unit price",
            "Invoice total",
            "€200.00",
            "Please transfer €200.00 by",
            "Payment reference",
        ):
            assert expected in html, expected
        for german in ("Rechnung", "Leistungsdatum", "Bitte überweisen", "Steuernummer"):
            assert german not in html, german
        pdf = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
        text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
        assert "Invoice" in text and "§ 19 UStG" in text

    def test_xml_notes_are_english(self, company, client_en, item):
        invoice = english_invoice(company, client_en, item)
        pdf = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
        _, xml = facturx.get_xml_from_pdf(pdf, check_xsd=False)
        root = etree.fromstring(xml)
        reason = root.xpath("string(//*[local-name()='ExemptionReason'])")
        terms = root.xpath("string(//*[local-name()='SpecifiedTradePaymentTerms']/*[local-name()='Description'])")
        assert reason.startswith("VAT exemption for small businesses")
        assert terms.startswith("Payable without deduction by")

    def test_cancellation_is_english(self, company, client_en, item):
        invoice = english_invoice(company, client_en, item)
        storno = cancel_invoice(invoice, issue_date=dt.date(2026, 10, 13), reason="Duplicate")
        assert storno.language == "en"
        html = render_document_html(storno)
        assert "Cancellation Invoice" in html and f"Cancellation of invoice {invoice.number}" in html
        assert "Reason: Duplicate" in html

    def test_default_mail_is_english(self, company, client_en, item):
        invoice = english_invoice(company, client_en, item)
        subject, body = default_message(invoice)
        assert subject == f"Invoice {invoice.number} from Muster Selbstständig"
        assert body.startswith("Hello,\n\nplease find attached our invoice")
        assert "Please transfer €200.00 by" in body and body.endswith("Kind regards\nMax Muster")

    def test_german_stays_unchanged(self, company, item):
        kunde = create_customer(company, name="Kunde GmbH", street="W 1", postcode="1", city="K")
        invoice = english_invoice(company, kunde, item)
        html = render_document_html(invoice)
        assert "Rechnung-Nr." in html and "Leistungsdatum" in html and "200,00 €" in html
        assert invoice.tax_note.startswith("Steuerbefreiung für Kleinunternehmer")


@pytest.mark.einvoice
def test_english_invoice_passes_official_validators(company, client_en, item, tmp_path):
    from skontio.einvoice.validation import ValidatorUnavailable, validate_with_kosit, validate_with_mustang

    invoice = english_invoice(company, client_en, item)
    pdf = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
    xml = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.XRECHNUNG_XML).blob)
    try:
        mustang = validate_with_mustang(pdf, workdir=tmp_path)
        kosit = validate_with_kosit(xml, workdir=tmp_path)
    except ValidatorUnavailable as exc:
        pytest.skip(str(exc))
    assert mustang.valid, mustang.summary
    assert kosit.valid, kosit.summary


class TestInterfaces:
    def test_header_form_switches_language(self, client, shop):
        client.force_login(shop.user)
        doc = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=dt.date(2026, 10, 12))
        client.post(f"/dokumente/{doc.pk}/", {"issue_date": "2026-10-12", "language": "en"})
        doc.refresh_from_db()
        assert doc.language == "en"

    def test_mcp_draft_language(self, ctx, shop):
        args = {
            "customer_number": shop.org.number,
            "idempotency_key": "language-test-1",
            "language": "en",
            "lines": [{"item_id": shop.item.pk, "quantity": "1"}],
        }
        result = call_tool(ctx, "invoice_draft_create", args)
        assert result["language"] == "en"
        changed = call_tool(ctx, "draft_update", {"document_id": result["document_id"], "language": "de"})
        assert changed["language"] == "de"

    def test_mcp_rejects_unknown_language(self, ctx, shop):
        from skontio.tools.registry import ToolError

        args = {
            "customer_number": shop.org.number,
            "idempotency_key": "language-test-2",
            "language": "fr",
            "lines": [{"item_id": shop.item.pk, "quantity": "1"}],
        }
        with pytest.raises(ToolError):
            call_tool(ctx, "invoice_draft_create", args)
