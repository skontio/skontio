"""Vorlagen (Layout, Farbe, Logo, Fußzeile), Textbausteine und PDF-Vorschau für Entwürfe."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from skontio.archive.services import load
from skontio.core.models import Company
from skontio.documents.layouts import TemplateError, delete_template, save_template, save_text_block
from skontio.documents.models import Document, DocumentTemplate, Rendition, TextBlock
from skontio.documents.renditions import render_document_html
from skontio.documents.services import add_line_from_item, cancel_invoice, create_draft, finalize, preview_pdf

D = Decimal


def png(size=(120, 40), mode="RGBA") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, (31, 95, 139, 128)).save(buffer, "PNG")
    return buffer.getvalue()


def invoice_draft(shop, template=None):
    doc = create_draft(
        shop.company,
        Document.Type.INVOICE,
        shop.org,
        issue_date=dt.date(2026, 10, 12),
        service_date=dt.date(2026, 10, 10),
    )
    add_line_from_item(doc, shop.item, quantity=D("2"))
    if template is not None:
        doc.template = template
        doc.save()
    return doc


class TestTemplates:
    def test_first_template_becomes_default_and_default_can_move(self, shop):
        first = save_template(shop.company, name="Klassisch")
        assert first.is_default
        second = save_template(shop.company, name="Modern", layout="modern", accent_color="#AA3300", is_default=True)
        first.refresh_from_db()
        assert second.is_default and not first.is_default
        assert second.accent_color == "#aa3300"
        second = save_template(shop.company, template=second, name="Modern", is_default=False)
        assert second.is_default  # unticking alone cannot leave the company without a default

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"name": ""}, "Namen"),
            ({"name": "x", "layout": "barock"}, "Layout"),
            ({"name": "x", "accent_color": "red"}, "#RRGGBB"),
            ({"name": "x", "accent_color": "#12345"}, "#RRGGBB"),
        ],
    )
    def test_invalid_input(self, shop, kwargs, message):
        with pytest.raises(TemplateError, match=message):
            save_template(shop.company, **kwargs)

    def test_logo_png_and_jpeg(self, shop):
        template = save_template(shop.company, name="Mit Logo", logo=png())
        assert template.logo.mime == "image/png"
        buffer = io.BytesIO()
        Image.new("RGB", (50, 50), "white").save(buffer, "JPEG")
        template = save_template(shop.company, template=template, name="Mit Logo", logo=buffer.getvalue())
        assert template.logo.mime == "image/jpeg"
        template = save_template(shop.company, template=template, name="Mit Logo", remove_logo=True)
        assert template.logo is None

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            (b"%PDF-1.7\n", "PNG oder JPEG"),
            (b"MZ\x90\x00", "PNG oder JPEG"),
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, "beschädigt"),
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * 1_000_001, "1 MB"),
        ],
    )
    def test_bad_logos_are_rejected(self, shop, data, message):
        with pytest.raises(TemplateError, match=message):
            save_template(shop.company, name="x", logo=data)

    def test_used_template_cannot_be_deleted(self, shop):
        used = save_template(shop.company, name="Benutzt")
        spare = save_template(shop.company, name="Reserve")
        finalize(invoice_draft(shop))
        with pytest.raises(TemplateError, match="bleibt erhalten"):
            delete_template(used)
        delete_template(spare)
        assert list(DocumentTemplate.objects.values_list("name", flat=True)) == ["Benutzt"]

    def test_deleting_the_default_promotes_another(self, shop):
        default = save_template(shop.company, name="A")
        other = save_template(shop.company, name="B")
        delete_template(default)
        other.refresh_from_db()
        assert other.is_default


class TestRendering:
    def test_finalize_records_the_template_and_renders_it(self, shop):
        template = save_template(
            shop.company,
            name="Modern",
            layout="modern",
            accent_color="#aa3300",
            footer_text="Inhaberin: Erika Muster · www.example.org",
            logo=png(),
        )
        invoice = finalize(invoice_draft(shop))
        assert invoice.template == template
        html = render_document_html(invoice)
        assert 'class="layout-modern"' in html and "--accent: #aa3300" in html
        assert "data:image/png;base64," in html and "Inhaberin: Erika Muster" in html
        assert "ENTWURF" not in html

    def test_a_chosen_template_wins_over_the_default(self, shop):
        save_template(shop.company, name="Standard")
        special = save_template(shop.company, name="Spezial", layout="modern")
        invoice = finalize(invoice_draft(shop, template=special))
        assert invoice.template == special

    def test_cancellation_keeps_the_invoice_template(self, shop):
        special = save_template(shop.company, name="Spezial", layout="modern")
        invoice = finalize(invoice_draft(shop, template=special))
        save_template(shop.company, name="Neu", is_default=True)
        storno = cancel_invoice(invoice, issue_date=dt.date(2026, 10, 13), reason="Fehler")
        assert storno.template == special

    def test_without_template_the_classic_layout_is_used(self, shop):
        invoice = finalize(invoice_draft(shop))
        assert invoice.template is None
        assert 'class="layout-classic"' in render_document_html(invoice)

    def test_editing_a_template_keeps_archived_pdfs(self, shop):
        template = save_template(shop.company, name="A", accent_color="#111111")
        invoice = finalize(invoice_draft(shop))
        before = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
        save_template(shop.company, template=template, name="A", accent_color="#222222")
        after = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
        assert before == after


class TestTextBlocks:
    def test_new_drafts_start_with_the_text_block(self, shop):
        save_text_block(shop.company, Document.Type.QUOTE, "de", intro_text="Gerne biete ich an:", closing_text="Gruß")
        save_text_block(shop.company, Document.Type.QUOTE, "en", intro_text="I am pleased to offer:", closing_text="")
        quote = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=dt.date(2026, 10, 12))
        assert (quote.intro_text, quote.closing_text) == ("Gerne biete ich an:", "Gruß")
        english = create_draft(
            shop.company, Document.Type.QUOTE, shop.org, issue_date=dt.date(2026, 10, 12), language="en"
        )
        assert (english.intro_text, english.closing_text) == ("I am pleased to offer:", "")
        invoice = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=dt.date(2026, 10, 12))
        assert invoice.intro_text == ""

    def test_explicit_text_wins(self, shop):
        save_text_block(shop.company, Document.Type.QUOTE, "de", intro_text="Standard", closing_text="Ende")
        quote = create_draft(
            shop.company, Document.Type.QUOTE, shop.org, issue_date=dt.date(2026, 10, 12), intro_text="Eigener Text"
        )
        assert (quote.intro_text, quote.closing_text) == ("Eigener Text", "Ende")

    def test_empty_block_is_removed(self, shop):
        save_text_block(shop.company, Document.Type.QUOTE, "de", intro_text="x", closing_text="")
        save_text_block(shop.company, Document.Type.QUOTE, "de", intro_text=" ", closing_text="")
        assert not TextBlock.objects.exists()


class TestPreview:
    def test_preview_pdf_of_a_draft(self, shop):
        pdf = preview_pdf(invoice_draft(shop))
        assert pdf.startswith(b"%PDF-")

    def test_preview_view(self, client, shop):
        client.force_login(shop.user)
        doc = invoice_draft(shop)
        response = client.get(f"/dokumente/{doc.pk}/vorschau.pdf")
        assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
        assert response["Cache-Control"] == "no-store"
        empty = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=dt.date(2026, 10, 12))
        assert client.get(f"/dokumente/{empty.pk}/vorschau.pdf").status_code == 409
        finalized = finalize(doc)
        assert client.get(f"/dokumente/{finalized.pk}/vorschau.pdf").status_code == 302

    def test_preview_is_marked_as_draft_without_girocode(self, shop):
        from skontio.documents.renditions import _view
        from skontio.documents.services import _buyer_data, _line_inputs, _regime, _seller_data

        doc = invoice_draft(shop)
        _, inputs = _line_inputs(doc, _regime(doc))
        view = _view(doc, draft=(inputs, _seller_data(shop.company), _buyer_data(shop.org)))
        assert view["draft"] and view["number"] == "(Entwurf)" and view["girocode"] == ""
        from django.template.loader import render_to_string

        html = render_to_string("documents/pdf.html", view)
        assert "ENTWURF – nicht gültig" in html and "Verwendungszweck" not in html and "None" not in html

    def test_other_companies_drafts_are_hidden(self, client, shop):
        other = Company.objects.create(name="Fremd")
        doc = create_draft(other, Document.Type.QUOTE, None, issue_date=dt.date(2026, 10, 12))
        client.force_login(shop.user)
        assert client.get(f"/dokumente/{doc.pk}/vorschau.pdf").status_code == 404


class TestWeb:
    def test_create_edit_and_list(self, client, shop):
        client.force_login(shop.user)
        logo = SimpleUploadedFile("logo.png", png(), content_type="image/png")
        response = client.post(
            "/einstellungen/vorlagen/neu/",
            {"name": "Hausfarbe", "layout": "modern", "accent_color": "#336699", "logo": logo},
        )
        assert response.status_code == 302
        template = DocumentTemplate.objects.get()
        assert template.logo is not None and template.is_default
        page = client.get("/einstellungen/vorlagen/").content.decode()
        assert "Hausfarbe" in page and 'fill="#336699"' in page
        edit = client.get(f"/einstellungen/vorlagen/{template.pk}/").content.decode()
        assert "data:image/png;base64," in edit
        client.post(
            f"/einstellungen/vorlagen/{template.pk}/",
            {"name": "Hausfarbe", "layout": "classic", "accent_color": "#336699", "is_default": "on"},
        )
        template.refresh_from_db()
        assert template.layout == "classic" and template.logo is not None

    def test_invalid_logo_shows_error(self, client, shop):
        client.force_login(shop.user)
        bad = SimpleUploadedFile("logo.png", b"%PDF-1.7", content_type="image/png")
        response = client.post(
            "/einstellungen/vorlagen/neu/", {"name": "x", "layout": "classic", "accent_color": "#336699", "logo": bad}
        )
        assert response.status_code == 400
        assert "PNG oder JPEG" in response.content.decode()

    def test_text_blocks_page(self, client, shop):
        client.force_login(shop.user)
        client.post("/einstellungen/textbausteine/", {"quote-de-intro": "Hallo", "quote-de-closing": "Tschüss"})
        block = TextBlock.objects.get()
        assert (block.doc_type, block.language, block.intro_text) == ("quote", "de", "Hallo")
        assert "Tschüss" in client.get("/einstellungen/textbausteine/").content.decode()

    def test_draft_header_offers_templates_of_the_own_company_only(self, client, shop):
        client.force_login(shop.user)
        doc = invoice_draft(shop)
        assert 'name="template"' not in client.get(f"/dokumente/{doc.pk}/").content.decode()
        own = save_template(shop.company, name="Eigene")
        other = Company.objects.create(name="Fremd")
        foreign = DocumentTemplate.objects.create(company=other, name="Fremde")
        assert "Eigene" in client.get(f"/dokumente/{doc.pk}/").content.decode()
        client.post(f"/dokumente/{doc.pk}/", {"issue_date": "2026-10-12", "template": foreign.pk})
        doc.refresh_from_db()
        assert doc.template is None
        client.post(
            f"/dokumente/{doc.pk}/", {"issue_date": "2026-10-12", "service_date": "2026-10-10", "template": own.pk}
        )
        doc.refresh_from_db()
        assert doc.template == own

    def test_only_admins(self, client, shop, django_user_model):
        client.force_login(django_user_model.objects.create_user("mitarbeiter"))
        assert client.get("/einstellungen/vorlagen/").status_code == 403


@pytest.mark.einvoice
def test_pdf_with_transparent_logo_is_valid_pdfa(shop, tmp_path):
    from skontio.einvoice.validation import ValidatorUnavailable, validate_with_mustang

    save_template(shop.company, name="Logo", layout="modern", logo=png(), footer_text="www.example.org")
    invoice = finalize(invoice_draft(shop))
    pdf = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
    try:
        result = validate_with_mustang(pdf, workdir=tmp_path)
    except ValidatorUnavailable as exc:
        pytest.skip(str(exc))
    assert result.valid, result.summary
