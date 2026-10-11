"""Dokumentfluss: Entwurf → Festschreiben → Angebot/Rechnung/Storno mit Steuerstatus-Wechsel."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.db import DatabaseError, connection, transaction

from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document, PartySnapshot
from skontio.documents.services import (
    DocumentError,
    DocumentLocked,
    add_line,
    add_line_from_item,
    cancel_invoice,
    confirm_ai_review,
    convert_quote_to_invoice,
    create_draft,
    finalize,
    preview_totals,
    redact_party_snapshot,
)
from skontio.tax.engine import Regime, TaxTreatment
from skontio.taxstatus.models import TaxStatusPeriod
from skontio.taxstatus.services import change_regime, start_regime

D = Decimal
Type = Document.Type


@pytest.fixture
def company(db):
    company = Company.objects.create(
        name="Muster Selbstständig",
        street="Musterweg 1",
        postcode="12345",
        city="Musterstadt",
        tax_number="123/456/78901",
        email="rechnung@example.org",
        iban="DE02120300000000202051",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def customer(company):
    return create_customer(company, name="Kunde GmbH", street="Kundenstraße 5", postcode="54321", city="Köln")


@pytest.fixture
def consulting(company):
    return create_item(company, name="Beratung", unit="HUR", net_price=D("80"))


def invoice_draft(company, customer, consulting, *, service_date=dt.date(2026, 10, 9), qty="3"):
    doc = create_draft(company, Type.INVOICE, customer, issue_date=dt.date(2026, 10, 11), service_date=service_date)
    add_line_from_item(doc, consulting, quantity=D(qty))
    return doc


@pytest.mark.django_db
class TestDrafts:
    def test_line_from_catalog_copies_item_data(self, company, customer, consulting):
        doc = invoice_draft(company, customer, consulting)
        line = doc.lines.get()
        assert (line.text, line.unit_code, line.unit_price, line.tax_treatment) == (
            "Beratung",
            "HUR",
            D("80.0000"),
            TaxTreatment.STANDARD,
        )
        consulting.net_price = D("95")
        consulting.save()
        assert doc.lines.get().unit_price == D("80.0000")

    def test_preview_uses_regime_of_service_date(self, company, customer, consulting):
        doc = invoice_draft(company, customer, consulting)
        totals = preview_totals(doc)
        assert (totals.tax_total, totals.grand_total) == (D("0.00"), D("240.00"))

    def test_customer_of_other_company_is_rejected(self, company, consulting):
        other = Company.objects.create(name="Andere")
        foreign = create_customer(other, name="Fremd", street="x", postcode="1", city="y")
        with pytest.raises(DocumentError):
            create_draft(company, Type.INVOICE, foreign, issue_date=dt.date(2026, 10, 11))


@pytest.mark.django_db
class TestFinalize:
    def test_finalize_snapshots_regime_taxes_totals_and_parties(self, company, customer, consulting):
        doc = finalize(invoice_draft(company, customer, consulting))
        assert doc.number == "RE-2026-0001"
        assert doc.regime == Regime.SMALL_BUSINESS
        assert (doc.net_total, doc.tax_total, doc.gross_total) == (D("240.00"), D("0.00"), D("240.00"))
        line = doc.lines.get()
        assert (line.tax_category, line.tax_rate, line.net_amount) == ("E", D("0.00"), D("240.00"))
        assert "§ 19 UStG" in doc.tax_note
        buyer = PartySnapshot.objects.get(document=doc, role=PartySnapshot.Role.BUYER)
        assert buyer.data["name"] == "Kunde GmbH"
        assert doc.buyer_snapshot_sha256 == buyer.sha256

    def test_quotes_have_their_own_number_range(self, company, customer, consulting):
        quote = create_draft(company, Type.QUOTE, customer, issue_date=dt.date(2026, 10, 11))
        add_line_from_item(quote, consulting, quantity=D("1"))
        assert finalize(quote).number == "AN-2026-0001"

    def test_service_date_is_mandatory_for_invoices(self, company, customer, consulting):
        doc = invoice_draft(company, customer, consulting)
        doc.service_date = None
        doc.save()
        with pytest.raises(DocumentError, match="Leistungsdatum"):
            finalize(doc)

    def test_customer_address_is_mandatory_for_invoices(self, company, consulting):
        bare = create_customer(company, name="Ohne Adresse")
        with pytest.raises(DocumentError, match="Anschrift"):
            finalize(invoice_draft(company, bare, consulting))

    def test_seller_tax_number_or_vat_id_is_mandatory(self, company, customer, consulting):
        Company.objects.filter(pk=company.pk).update(tax_number="", vat_id="")
        company.refresh_from_db()
        with pytest.raises(DocumentError, match="Steuernummer"):
            finalize(invoice_draft(company, customer, consulting))

    def test_service_period_across_regime_change_must_be_split(self, company, customer, consulting):
        change_regime(company, Regime.STANDARD, dt.date(2026, 8, 17), TaxStatusPeriod.Reason.THRESHOLD_CURRENT_YEAR)
        doc = create_draft(
            company,
            Type.INVOICE,
            customer,
            issue_date=dt.date(2026, 10, 11),
            service_period=(dt.date(2026, 8, 1), dt.date(2026, 8, 31)),
        )
        add_line_from_item(doc, consulting, quantity=D("1"))
        with pytest.raises(DocumentError, match="aufteilen"):
            finalize(doc)


@pytest.mark.django_db
class TestRegimeChange:
    def test_finalized_documents_keep_their_taxes(self, company, customer, consulting):
        before = finalize(invoice_draft(company, customer, consulting, service_date=dt.date(2026, 8, 10)))
        change_regime(company, Regime.STANDARD, dt.date(2026, 8, 17), TaxStatusPeriod.Reason.THRESHOLD_CURRENT_YEAR)
        before.refresh_from_db()
        assert (before.regime, before.gross_total) == (Regime.SMALL_BUSINESS, D("240.00"))

        after = finalize(invoice_draft(company, customer, consulting, service_date=dt.date(2026, 8, 20)))
        assert after.regime == Regime.STANDARD
        assert (after.tax_total, after.gross_total) == (D("45.60"), D("285.60"))
        assert after.tax_note == ""

    def test_draft_created_before_change_is_recalculated(self, company, customer, consulting):
        draft = invoice_draft(company, customer, consulting, service_date=dt.date(2026, 9, 1))
        assert preview_totals(draft).tax_total == D("0.00")
        change_regime(company, Regime.STANDARD, dt.date(2026, 8, 17), TaxStatusPeriod.Reason.THRESHOLD_CURRENT_YEAR)
        assert preview_totals(draft).tax_total == D("45.60")


@pytest.mark.django_db
class TestQuoteToInvoice:
    def test_conversion_copies_lines_and_links_documents(self, company, customer, consulting):
        quote = create_draft(company, Type.QUOTE, customer, issue_date=dt.date(2026, 9, 1))
        add_line_from_item(quote, consulting, quantity=D("5"), discount_percent=D("10"))
        finalize(quote)
        invoice = convert_quote_to_invoice(quote, issue_date=dt.date(2026, 10, 11), service_date=dt.date(2026, 10, 9))
        assert (invoice.doc_type, invoice.status, invoice.parent_id) == (Type.INVOICE, Document.Status.DRAFT, quote.pk)
        line = invoice.lines.get()
        assert (line.quantity, line.discount_percent) == (D("5.0000"), D("10.00"))

    def test_only_finalized_quotes_can_be_converted(self, company, customer, consulting):
        quote = create_draft(company, Type.QUOTE, customer, issue_date=dt.date(2026, 9, 1))
        add_line_from_item(quote, consulting, quantity=D("1"))
        with pytest.raises(DocumentError):
            convert_quote_to_invoice(quote, issue_date=dt.date(2026, 10, 11), service_date=dt.date(2026, 10, 9))


@pytest.mark.django_db
class TestCancellation:
    def test_storno_mirrors_original_taxes_even_after_regime_change(self, company, customer, consulting):
        original = finalize(invoice_draft(company, customer, consulting, service_date=dt.date(2026, 8, 10)))
        change_regime(company, Regime.STANDARD, dt.date(2026, 8, 17), TaxStatusPeriod.Reason.THRESHOLD_CURRENT_YEAR)
        storno = cancel_invoice(original, issue_date=dt.date(2026, 10, 12), reason="Doppelt gestellt")
        assert storno.doc_type == Type.CANCELLATION
        assert storno.status == Document.Status.FINALIZED
        assert storno.parent_id == original.pk
        assert (storno.regime, storno.gross_total) == (Regime.SMALL_BUSINESS, D("240.00"))
        assert storno.number == "ST-2026-0001"

    def test_invoice_can_be_cancelled_only_once(self, company, customer, consulting):
        original = finalize(invoice_draft(company, customer, consulting))
        cancel_invoice(original, issue_date=dt.date(2026, 10, 12), reason="Fehler")
        with pytest.raises(DocumentError):
            cancel_invoice(original, issue_date=dt.date(2026, 10, 13), reason="nochmal")

    def test_drafts_cannot_be_cancelled(self, company, customer, consulting):
        with pytest.raises(DocumentError):
            cancel_invoice(invoice_draft(company, customer, consulting), issue_date=dt.date(2026, 10, 12), reason="x")


@pytest.mark.django_db
class TestAiReview:
    def test_ai_drafts_need_human_review_before_finalizing(self, company, customer, consulting, django_user_model):
        doc = create_draft(
            company,
            Type.INVOICE,
            customer,
            issue_date=dt.date(2026, 10, 11),
            service_date=dt.date(2026, 10, 9),
            origin=Document.Origin.MCP,
        )
        add_line(doc, text="Beratung", quantity=D("1"), unit_code="HUR", unit_price=D("80"))
        with pytest.raises(DocumentError, match="Prüfung"):
            finalize(doc)
        confirm_ai_review(doc, django_user_model.objects.create_user("inhaber"))
        assert finalize(doc).status == Document.Status.FINALIZED


@pytest.mark.django_db
class TestRedaction:
    def test_party_snapshot_can_be_redacted_without_touching_the_locked_document(self, company, customer, consulting):
        doc = finalize(invoice_draft(company, customer, consulting))
        digest = doc.buyer_snapshot_sha256
        redact_party_snapshot(doc, PartySnapshot.Role.BUYER)
        snapshot = PartySnapshot.objects.get(document=doc, role=PartySnapshot.Role.BUYER)
        assert snapshot.data == {"redacted": True}
        assert snapshot.redacted_at is not None
        doc.refresh_from_db()
        assert doc.buyer_snapshot_sha256 == digest  # proof of the original content remains


@pytest.mark.django_db
def test_lock_triggers_survive_schema_changes(company, customer, consulting):
    """SQLite rebuilds tables on ALTER; post_migrate must reinstall the lock triggers."""
    doc = finalize(invoice_draft(company, customer, consulting))
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE documents_document SET gross_total = 1 WHERE id = %s", [doc.pk])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE documents_documentline SET net_amount = 1 WHERE document_id = %s", [doc.pk])
    with pytest.raises(DocumentLocked):
        doc.save()
