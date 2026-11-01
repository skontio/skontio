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


@pytest.mark.django_db
class TestInlineLineEdit:
    def test_edit_row_and_save(self, app, draft):
        line = draft.lines.get()
        row = app.get(f"/dokumente/{draft.pk}/positionen/{line.pk}/", HTTP_HX_REQUEST="true").content.decode()
        assert 'class="editing"' in row and 'value="3"' in row and 'value="80"' in row
        response = app.post(
            f"/dokumente/{draft.pk}/positionen/{line.pk}/",
            {"text": "Beratung vor Ort", "quantity": "2,5", "unit_price": "90", "discount_percent": ""},
            HTTP_HX_REQUEST="true",
        )
        line.refresh_from_db()
        assert (line.text, line.quantity, line.unit_price) == ("Beratung vor Ort", D("2.5"), D("90"))
        body = response.content.decode()
        assert "225,00" in body and 'id="draft-total" hx-swap-oob="true"' in body

    def test_invalid_input_keeps_line(self, app, draft):
        line = draft.lines.get()
        body = app.post(
            f"/dokumente/{draft.pk}/positionen/{line.pk}/",
            {"text": "", "quantity": "1", "unit_price": "1"},
            HTTP_HX_REQUEST="true",
        ).content.decode()
        line.refresh_from_db()
        assert line.text == "Beratung" and 'role="alert"' in body

    def test_cancel_returns_table(self, app, draft):
        line = draft.lines.get()
        body = app.get(f"/dokumente/{draft.pk}/positionen/{line.pk}/?abbrechen=1").content.decode()
        assert 'id="lines"' in body and "editing" not in body

    def test_finalized_lines_are_locked(self, app, draft):
        line = draft.lines.get()
        doc = finalize(draft)
        assert app.get(f"/dokumente/{doc.pk}/positionen/{line.pk}/").status_code == 409


@pytest.mark.django_db
class TestDocumentStatus:
    def test_list_shows_stamps_and_due_date(self, app, draft):
        draft_body = app.get("/dokumente/").content.decode()
        assert "stamp draft" in draft_body
        doc = finalize(draft)
        assert "stamp overdue" in app.get("/dokumente/").content.decode()  # due 25.10.2026, today is later
        Payment.objects.create(company=doc.company, document=doc, amount=doc.gross_total, received_on=doc.issue_date)
        body = app.get("/dokumente/").content.decode()
        assert "stamp paid" in body and doc.due_date.strftime("%d.%m.%Y") not in body.split("<tbody")[1]

    def test_search_by_customer_or_number(self, app, draft, company):
        other = create_customer(company, name="Andere AG")
        create_draft(company, Document.Type.QUOTE, other, issue_date=dt.date(2026, 10, 1))
        rows = app.get("/dokumente/", {"q": "andere"}).content.decode().split("<tbody")[1]
        assert "Andere AG" in rows and "Kunde GmbH" not in rows
