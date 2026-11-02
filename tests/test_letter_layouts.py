"""Vier Designs, ein Raster: alle Dokumente einer Firma sehen gleich aus und halten DIN 5008 ein."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from PIL import Image
from pypdf import PdfReader

from skontio.archive.services import load
from skontio.documents.layouts import TemplateError, contrast_to_white, save_template, soft
from skontio.documents.models import Document, DocumentTemplate, Rendition
from skontio.documents.renditions import render_document_html
from skontio.documents.services import add_line, add_line_from_item, create_draft, finalize
from skontio.dunning.services import create_notice

DAY = dt.date(2026, 3, 2)
LAYOUTS = [value for value, _ in DocumentTemplate.Layout.choices]


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (120, 40), "#2d6a4f").save(buffer, "PNG")
    return buffer.getvalue()


def invoice(shop, lines=1):
    doc = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=DAY, service_date=DAY)
    for i in range(lines):
        add_line(doc, text=f"Leistung {i + 1}", quantity=Decimal("1"), unit_code="C62", unit_price=Decimal("10"))
    return finalize(doc)


def pdf_text(blob) -> str:
    return "".join(page.extract_text() for page in PdfReader(io.BytesIO(load(blob))).pages)


def test_four_layouts_exist():
    assert LAYOUTS == ["classic", "modern", "minimal", "sidebar"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_every_layout_renders_offer_invoice_and_dunning_letter(shop, layout):
    save_template(shop.company, name="Standard", layout=layout, accent_color="#2d6a4f", is_default=True, logo=png())
    quote = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=DAY)
    add_line_from_item(quote, shop.item, quantity=Decimal("1"))
    for doc in (finalize(quote), invoice(shop)):
        html = render_document_html(doc)
        assert f'class="layout-{layout}"' in html and 'class="fold fold-1"' in html and "data:image/png;base64" in html
    notice = create_notice(invoice(shop), issued_on=dt.date(2026, 4, 30))
    assert notice.number in pdf_text(notice.blob)


def test_long_invoices_number_their_pages(shop):
    doc = invoice(shop, lines=45)
    pdf = PdfReader(io.BytesIO(load(doc.renditions.get(kind=Rendition.Kind.ZUGFERD_PDF).blob)))
    assert len(pdf.pages) >= 2
    second = pdf.pages[1].extract_text()
    assert f"Rechnung {doc.number}" in second and f"Seite 2 von {len(pdf.pages)}" in second
    assert "Seite 1 von" not in pdf.pages[0].extract_text()  # the first page carries the letterhead instead


def test_english_page_labels(shop):
    shop.org.language = "en"
    shop.org.save()
    assert '" · Page " counter(page) " of " counter(pages)' in render_document_html(invoice(shop))


def test_mandatory_details_on_dunning_letters_too(shop):
    company = shop.company
    company.legal_form, company.register_court, company.register_number = "gmbh", "AG Musterstadt", "HRB 999"
    company.managing_directors = "Erika Muster"
    company.save()
    notice = create_notice(invoice(shop), issued_on=dt.date(2026, 4, 30))
    text = pdf_text(notice.blob)
    assert "HRB 999" in text and "Erika Muster" in " ".join(text.split())  # the footer wraps lines


def test_too_light_accent_colours_are_refused(shop):
    assert contrast_to_white("#ffffff") == pytest.approx(1.0)
    assert contrast_to_white("#000000") == pytest.approx(21.0)
    for colour in ("#ffff00", "#f3d34a", "#dddddd"):
        with pytest.raises(TemplateError, match="zu hell"):
            save_template(shop.company, name="X", accent_color=colour)
    assert save_template(shop.company, name="X", accent_color="#1f5f8b").accent_color == "#1f5f8b"
    assert soft("#000000") == "#e6e6e6" and soft("#ffffff") == "#ffffff"


def test_wizard_shows_every_layout_with_the_logo(client, shop):
    save_template(shop.company, name="Standard", layout="sidebar", accent_color="#5b3c88", is_default=True, logo=png())
    client.force_login(shop.user)
    page = client.get("/einrichtung/assistent/aussehen/").content.decode()
    assert page.count('class="layout-preview"') == 4 and page.count("data:image/png;base64") >= 4
    assert 'value="sidebar" checked' in page and 'fill="#5b3c88"' in page
    assert " style=" not in page  # CSP: no inline styles


def test_wizard_preview_button_saves_without_finishing_the_step(client, shop):
    client.force_login(shop.user)
    data = {"accent_color": "#7a3e12", "layout": "minimal", "aktion": "vorschau"}
    response = client.post("/einrichtung/assistent/aussehen/", data)
    assert response["Location"] == "/einrichtung/assistent/aussehen/"
    shop.company.refresh_from_db()
    assert "aussehen" not in (shop.company.onboarding or {})
    template = DocumentTemplate.objects.get(company=shop.company, is_default=True)
    assert (template.layout, template.accent_color) == ("minimal", "#7a3e12")
    response = client.post("/einrichtung/assistent/aussehen/", {"accent_color": "#7a3e12", "layout": "unbekannt"})
    assert response.status_code == 200
