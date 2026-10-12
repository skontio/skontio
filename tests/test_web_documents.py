"""Oberfläche: Dokument-Editor, Festschreiben, Downloads, Zahlung, Storno, Angebot → Rechnung."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.payments.models import Payment
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime

D = Decimal


@pytest.fixture
def company(db):
    company = Company.objects.create(
        name="Muster Selbstständig",
        street="Musterweg 1",
        postcode="12345",
        city="Musterstadt",
        tax_number="123/456/78901",
        iban="DE02120300000000202051",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def customer(company):
    return create_customer(company, name="Kunde GmbH", street="Weg 5", postcode="54321", city="Köln")


@pytest.fixture
def item(company):
    return create_item(company, name="Beratung", unit="HUR", net_price=D("80"))


@pytest.fixture
def app(client, django_user_model, company):
    client.force_login(django_user_model.objects.create_user("inhaber", is_staff=True))
    return client


@pytest.fixture
def draft(company, customer, item):
    doc = create_draft(
        company, Document.Type.INVOICE, customer, issue_date=dt.date(2026, 10, 11), service_date=dt.date(2026, 10, 9)
    )
    add_line_from_item(doc, item, quantity=D("3"))
    return doc


@pytest.mark.django_db
class TestDraftEditing:
    def test_create_draft(self, app, customer):
        response = app.post(
            "/dokumente/neu/",
            {"doc_type": "invoice", "customer": customer.pk, "issue_date": "11.10.2026", "service_date": "09.10.2026"},
        )
        doc = Document.objects.get()
        assert response.status_code == 302 and response["Location"] == f"/dokumente/{doc.pk}/"
        assert (doc.status, doc.customer, doc.service_date) == ("draft", customer, dt.date(2026, 10, 9))

    def test_new_form_preselects_type_and_customer(self, app, customer):
        response = app.get("/dokumente/neu/", {"typ": "quote", "kunde": customer.pk})
        form = response.context["form"]
        assert (form.initial["doc_type"], form.initial["customer"]) == ("quote", customer.pk)

    def test_editor_shows_lines_and_preview_totals(self, app, draft):
        body = app.get(f"/dokumente/{draft.pk}/").content.decode()
        assert "Beratung" in body and "240,00" in body and "§ 19 UStG" in body

    def test_add_line_from_catalog_returns_fragment(self, app, draft, item):
        response = app.post(
            f"/dokumente/{draft.pk}/positionen/", {"item": item.pk, "quantity": "1,5"}, HTTP_HX_REQUEST="true"
        )
        assert response.status_code == 200
        assert "<html" not in response.content.decode()
        assert draft.lines.count() == 2
        assert draft.lines.last().quantity == D("1.5")

    def test_add_free_line(self, app, draft):
        app.post(
            f"/dokumente/{draft.pk}/positionen/",
            {"text": "Fahrtkosten", "quantity": "42", "unit_code": "KMT", "unit_price": "0,30"},
        )
        line = draft.lines.last()
        assert (line.text, line.unit_code, line.unit_price) == ("Fahrtkosten", "KMT", D("0.3000"))

    def test_invalid_line_shows_error(self, app, draft):
        response = app.post(f"/dokumente/{draft.pk}/positionen/", {"text": "", "quantity": "1"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert "Bezeichnung" in response.content.decode()
        assert draft.lines.count() == 1

    def test_remove_line(self, app, draft):
        line = draft.lines.get()
        app.post(f"/dokumente/{draft.pk}/positionen/{line.pk}/entfernen/")
        assert draft.lines.count() == 0

    def test_update_header(self, app, draft):
        app.post(
            f"/dokumente/{draft.pk}/",
            {
                "issue_date": "12.10.2026",
                "service_date": "10.10.2026",
                "due_date": "26.10.2026",
                "document_discount_percent": "5",
                "intro_text": "Hallo",
                "closing_text": "Danke",
            },
        )
        draft.refresh_from_db()
        assert (draft.issue_date, draft.document_discount_percent, draft.intro_text) == (
            dt.date(2026, 10, 12),
            D("5.00"),
            "Hallo",
        )

    def test_delete_draft(self, app, draft):
        assert app.post(f"/dokumente/{draft.pk}/loeschen/").status_code == 302
        assert not Document.objects.exists()

    def test_documents_of_other_company_are_hidden(self, app, company):
        other = Company.objects.create(name="Fremd")
        foreign = Document.objects.create(company=other, doc_type="invoice", issue_date=dt.date(2026, 1, 1))
        assert app.get(f"/dokumente/{foreign.pk}/").status_code == 404

    def test_list_filters_by_type(self, app, draft, company, customer):
        create_draft(company, Document.Type.QUOTE, customer, issue_date=dt.date(2026, 10, 1))
        body = app.get("/dokumente/", {"typ": "quote"}).content.decode()
        rows = body.split("<tbody")[1].split("</tbody>")[0]
        assert "Angebot" in rows and "Rechnung" not in rows


@pytest.mark.django_db
class TestFinalizedDocuments:
    def test_finalize_and_download(self, app, draft):
        response = app.post(f"/dokumente/{draft.pk}/festschreiben/")
        assert response.status_code == 302
        draft.refresh_from_db()
        assert draft.status == "finalized"
        pdf = app.get(f"/dokumente/{draft.pk}/datei/zugferd_pdf/")
        assert pdf["Content-Type"] == "application/pdf"
        assert 'filename="RE-2026-0001.pdf"' in pdf["Content-Disposition"]
        assert b"".join(pdf.streaming_content).startswith(b"%PDF")

    def test_finalize_error_is_shown_and_draft_kept(self, app, draft):
        Document.objects.filter(pk=draft.pk).update(service_date=None)
        response = app.post(f"/dokumente/{draft.pk}/festschreiben/")
        assert response.status_code == 200
        assert "Leistungsdatum" in response.content.decode()
        draft.refresh_from_db()
        assert draft.status == "draft"

    def test_finalized_document_cannot_be_edited(self, app, draft):
        doc = finalize(draft)
        assert app.post(f"/dokumente/{doc.pk}/positionen/", {"text": "x", "quantity": "1"}).status_code == 409
        assert app.post(f"/dokumente/{doc.pk}/loeschen/").status_code == 409

    def test_record_payment(self, app, draft):
        doc = finalize(draft)
        app.post(
            f"/dokumente/{doc.pk}/zahlung/",
            {"amount": "100,00", "received_on": "20.10.2026", "method": "bank_transfer"},
        )
        assert Payment.objects.get().amount == D("100.00")
        assert "140,00" in app.get(f"/dokumente/{doc.pk}/").content.decode()  # open amount

    def test_cancel(self, app, draft):
        doc = finalize(draft)
        response = app.post(f"/dokumente/{doc.pk}/stornieren/", {"reason": "Doppelt gestellt"})
        storno = Document.objects.get(doc_type="cancellation")
        assert response["Location"] == f"/dokumente/{storno.pk}/"

    def test_quote_to_invoice(self, app, company, customer, item):
        quote = create_draft(company, Document.Type.QUOTE, customer, issue_date=dt.date(2026, 10, 1))
        add_line_from_item(quote, item, quantity=D("2"))
        quote = finalize(quote)
        response = app.post(f"/dokumente/{quote.pk}/in-rechnung/", {"service_date": "09.10.2026"})
        invoice = Document.objects.get(doc_type="invoice")
        assert response["Location"] == f"/dokumente/{invoice.pk}/"
        assert invoice.parent_id == quote.pk


@pytest.mark.django_db
def test_ai_draft_review(app, company, customer, item):
    doc = create_draft(
        company,
        Document.Type.INVOICE,
        customer,
        issue_date=dt.date(2026, 10, 11),
        service_date=dt.date(2026, 10, 9),
        origin=Document.Origin.MCP,
    )
    add_line_from_item(doc, item, quantity=D("1"))
    assert "KI-Entwurf" in app.get(f"/dokumente/{doc.pk}/").content.decode()
    app.post(f"/dokumente/{doc.pk}/pruefen/")
    doc.refresh_from_db()
    assert doc.ai_reviewed_at is not None
