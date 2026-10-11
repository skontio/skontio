"""HTML → PDF/A-3b via WeasyPrint. PDF/A-3 is required to embed the e-invoice XML."""

from __future__ import annotations

from html import escape

from weasyprint import HTML


def render_pdf_a3(body_html: str, *, title: str, lang: str = "de") -> bytes:
    document = (
        f'<!doctype html><html lang="{escape(lang)}"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title></head><body>{body_html}</body></html>"
    )
    return render_html_pdf_a3(document)


def render_html_pdf_a3(html: str) -> bytes:
    """Render a complete HTML document (template output) to PDF/A-3b."""
    return HTML(string=html).write_pdf(pdf_variant="pdf/a-3b")
