"""Renditions created while finalizing: PDF/A-3 with ZUGFeRD XML, XRechnung XML, plain PDF/A.

Runs inside the finalize transaction: if the mandatory e-invoice cannot be produced, the
document is not finalized. Everything is rendered from the snapshot only.
"""

from __future__ import annotations

from decimal import Decimal

from django.template.loader import render_to_string

from skontio.archive.services import retention_end, store
from skontio.documents.models import INVOICE_TYPES, Document, PartySnapshot, Rendition
from skontio.einvoice.cii import EInvoiceDataError, Profile, build_cii
from skontio.einvoice.hybrid import embed_xml
from skontio.einvoice.mapping import invoice_from_document
from skontio.rendering.girocode import girocode_svg_data_uri
from skontio.rendering.pdf import render_html_pdf_a3
from skontio.tax.engine import LineInput, ResolvedTax, calculate

Type = Document.Type
INVOICE_RETENTION_YEARS = 8  # Buchungsbelege, § 147 Abs. 3 AO
LETTER_RETENTION_YEARS = 6  # Handels- und Geschäftsbriefe
TITLES = {
    Type.QUOTE: "Angebot",
    Type.ORDER_CONFIRMATION: "Auftragsbestätigung",
    Type.INVOICE: "Rechnung",
    Type.PARTIAL_INVOICE: "Abschlagsrechnung",
    Type.FINAL_INVOICE: "Schlussrechnung",
    Type.CANCELLATION: "Stornorechnung",
}
UNIT_LABELS = {"C62": "Stk.", "HUR": "Std.", "DAY": "Tag(e)", "MON": "Monat(e)", "LS": "pauschal", "KMT": "km"}


class RenditionError(ValueError):
    pass


def de_amount(value: Decimal, places: int = 2) -> str:
    text = f"{value:,.{places}f}"
    return text.replace(",", " ").replace(".", ",").replace(" ", ".")


def de_quantity(value: Decimal) -> str:
    normalized = value.normalize()
    places = max(0, -normalized.as_tuple().exponent)
    return de_amount(value, places)


def de_percent(value: Decimal) -> str:
    return f"{de_quantity(value)} %"


def _view(document: Document) -> dict:
    snapshots = {s.role: s.data for s in document.party_snapshots.all()}
    seller, buyer = snapshots[PartySnapshot.Role.SELLER], snapshots[PartySnapshot.Role.BUYER]
    lines = list(document.lines.all())
    totals = calculate(
        [
            LineInput(
                line.quantity,
                line.unit_price,
                ResolvedTax(line.tax_category, line.tax_rate, line.tax_exemption_reason),
                line.discount_percent,
            )
            for line in lines
        ],
        document_discount_percent=document.document_discount_percent,
    )
    show_rates = totals.tax_total != 0 or any(b.category == "S" for b in totals.breakdown)
    doc_type = Type(document.doc_type)
    if document.service_period_start and document.service_period_end:
        service_label = "Leistungszeitraum"
        service = f"{document.service_period_start:%d.%m.%Y} – {document.service_period_end:%d.%m.%Y}"
    else:
        service_label = "Leistungsdatum"
        service = f"{document.service_date:%d.%m.%Y}" if document.service_date else ""
    reference = ""
    if doc_type == Type.CANCELLATION and document.parent:
        reference = f"Stornierung der Rechnung {document.parent.number} vom {document.parent.issue_date:%d.%m.%Y}."
        if document.cancellation_reason:
            reference += f" Grund: {document.cancellation_reason}"

    if doc_type == Type.PARTIAL_INVOICE and document.parent:
        reference = f"Abschlagsrechnung zum Angebot {document.parent.number} vom {document.parent.issue_date:%d.%m.%Y}."
    if doc_type == Type.FINAL_INVOICE and document.parent:
        reference = f"Schlussrechnung zum Angebot {document.parent.number} vom {document.parent.issue_date:%d.%m.%Y}."

    deductions = list(document.deductions.select_related("source"))
    due = totals.grand_total - sum((d.gross_amount for d in deductions), Decimal("0"))
    payment_text, girocode = "", ""
    if doc_type in INVOICE_TYPES and due > 0 and document.due_date:
        payment_text = (
            document.payment_terms_text
            or f"Bitte überweisen Sie {de_amount(due)} € bis zum {document.due_date:%d.%m.%Y}."
        )
        if seller.get("iban") and document.currency == "EUR":
            girocode = girocode_svg_data_uri(
                name=seller.get("account_holder") or seller["name"],
                iban=seller["iban"],
                bic=seller.get("bic", ""),
                amount=due,
                reference=document.number,
            )

    iban = seller.get("iban", "")
    return {
        "doc": document,
        "title": TITLES[doc_type],
        "language": document.language,
        "accent_color": "#1f5f8b",
        "seller": seller,
        "buyer": buyer,
        "iban_formatted": " ".join(iban[i : i + 4] for i in range(0, len(iban), 4)),
        "issue_date": f"{document.issue_date:%d.%m.%Y}",
        "service_label": service_label,
        "service": service,
        "due_date": f"{document.due_date:%d.%m.%Y}" if document.due_date and doc_type in INVOICE_TYPES else "",
        "valid_until": f"{document.valid_until:%d.%m.%Y}" if document.valid_until and doc_type == Type.QUOTE else "",
        "reference": reference,
        "show_rates": show_rates,
        "has_discounts": any(line.discount_percent for line in lines),
        "doc_discount": de_percent(document.document_discount_percent),
        "lines": [
            {
                "position": line.position,
                "text": line.text,
                "description": line.description,
                "quantity": de_quantity(line.quantity),
                "unit": UNIT_LABELS.get(line.unit_code, line.unit_code),
                "unit_price": de_amount(line.unit_price),
                "discount": de_percent(line.discount_percent) if line.discount_percent else "",
                "net": de_amount(result.net_amount),
                "rate": de_percent(line.tax_rate),
            }
            for line, result in zip(lines, totals.lines, strict=True)
        ],
        "subtotals": [
            {
                "rate": de_percent(b.rate),
                "basis": de_amount(b.basis),
                "tax": de_amount(b.tax),
                "allowance": de_amount(b.allowance) if b.allowance else "",
            }
            for b in totals.breakdown
        ],
        "totals": {
            "line_total": de_amount(totals.line_total),
            "tax_basis_total": de_amount(totals.tax_basis_total),
            "grand_total": de_amount(totals.grand_total),
        },
        "grand_label": "Gesamtbetrag" if show_rates else "Rechnungsbetrag",
        "tax_notes": [note for note in document.tax_note.split("\n") if note],
        "deductions": [
            {
                "number": d.source.number,
                "date": f"{d.source.issue_date:%d.%m.%Y}",
                "net": de_amount(d.net_amount),
                "rate": de_percent(d.tax_rate),
                "tax": de_amount(d.tax_amount),
                "gross": de_amount(d.gross_amount),
            }
            for d in deductions
        ],
        "due_total": de_amount(due),
        "due_label": "Gutschrift (Restbetrag)" if doc_type == Type.CANCELLATION else "Noch zu zahlen",
        "payment_text": payment_text,
        "girocode": girocode,
    }


def render_document_html(document: Document) -> str:
    return render_to_string("documents/pdf.html", _view(document))


def _add(document: Document, kind: Rendition.Kind, data: bytes, mime: str, years: int) -> Rendition:
    blob = store(data, mime=mime, retention_until=retention_end(document.issue_date, years))
    return Rendition.objects.create(document=document, kind=kind, blob=blob)


def create_renditions(document: Document) -> list[Rendition]:
    pdf = render_html_pdf_a3(render_document_html(document))
    if document.doc_type in (Type.QUOTE, Type.ORDER_CONFIRMATION):
        return [_add(document, Rendition.Kind.PDF, pdf, "application/pdf", LETTER_RETENTION_YEARS)]

    invoice = invoice_from_document(document)
    try:
        en16931 = build_cii(invoice, Profile.EN16931)
    except EInvoiceDataError as exc:
        raise RenditionError(f"E-Rechnung (ZUGFeRD) kann nicht erzeugt werden: {exc}") from exc
    renditions = [
        _add(document, Rendition.Kind.ZUGFERD_PDF, embed_xml(pdf, en16931), "application/pdf", INVOICE_RETENTION_YEARS)
    ]
    try:
        xrechnung = build_cii(invoice, Profile.XRECHNUNG)
    except EInvoiceDataError:
        return renditions  # optional: XRechnung needs buyer e-mail, buyer reference and seller contact
    renditions.append(
        _add(document, Rendition.Kind.XRECHNUNG_XML, xrechnung, "application/xml", INVOICE_RETENTION_YEARS)
    )
    return renditions
