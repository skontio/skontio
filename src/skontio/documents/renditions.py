"""Renditions created while finalizing: PDF/A-3 with ZUGFeRD XML, XRechnung XML, plain PDF/A.

Runs inside the finalize transaction: if the mandatory e-invoice cannot be produced, the
document is not finalized. Everything is rendered from the snapshot only.
"""

from __future__ import annotations

from decimal import Decimal

from django.template.loader import render_to_string

from skontio.archive.services import retention_end, store
from skontio.documents.languages import TEXTS, Lang
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
_DE = Lang("de")
TITLES = TEXTS["de"]["titles"]  # German UI and protocol texts
UNIT_LABELS = TEXTS["de"]["units"]
de_amount, de_quantity, de_percent = _DE.amount, _DE.quantity, _DE.percent


class RenditionError(ValueError):
    pass


def _view(document: Document) -> dict:
    t = Lang(document.language)
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
    title = t.title(doc_type)
    if document.service_period_start and document.service_period_end:
        service_label = t("service_period")
        service = f"{t.date(document.service_period_start)} – {t.date(document.service_period_end)}"
    else:
        service_label = t("service_date")
        service = t.date(document.service_date)
    reference = ""
    if doc_type == Type.CANCELLATION and document.parent:
        parent = document.parent
        reference = t("cancellation_of", number=parent.number, date=t.date(parent.issue_date)) + "."
        if document.cancellation_reason:
            reference += " " + t("reason", reason=document.cancellation_reason)
    if doc_type == Type.PARTIAL_INVOICE and document.parent:
        reference = t("partial_of", basis=t.basis(document.parent)) + "."
    if doc_type == Type.FINAL_INVOICE and document.parent:
        reference = t("final_of", basis=t.basis(document.parent)) + "."

    deductions = list(document.deductions.select_related("source"))
    due = totals.grand_total - sum((d.gross_amount for d in deductions), Decimal("0"))
    payment_text, girocode = "", ""
    if doc_type in INVOICE_TYPES and due > 0 and document.due_date:
        payment_text = document.payment_terms_text or t("pay_by", amount=t.money(due), date=t.date(document.due_date))
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
        "t": t.texts,
        "title": title,
        "number_label": t("number", title=title),
        "language": t.code,
        "accent_color": "#1f5f8b",
        "seller": seller,
        "buyer": buyer,
        "iban_formatted": " ".join(iban[i : i + 4] for i in range(0, len(iban), 4)),
        "issue_date": t.date(document.issue_date),
        "service_label": service_label,
        "service": service,
        "due_date": t.date(document.due_date) if doc_type in INVOICE_TYPES else "",
        "valid_until": t.date(document.valid_until) if doc_type == Type.QUOTE else "",
        "reference": reference,
        "show_rates": show_rates,
        "has_discounts": any(line.discount_percent for line in lines),
        "doc_discount": t.percent(document.document_discount_percent),
        "lines": [
            {
                "position": line.position,
                "text": line.text,
                "description": line.description,
                "quantity": t.quantity(line.quantity),
                "unit": t.unit(line.unit_code),
                "unit_price": t.amount(line.unit_price),
                "discount": t.percent(line.discount_percent) if line.discount_percent else "",
                "net": t.amount(result.net_amount),
                "rate": t.percent(line.tax_rate),
            }
            for line, result in zip(lines, totals.lines, strict=True)
        ],
        "subtotals": [
            {
                "rate": t.percent(b.rate),
                "vat_on": t("vat_on", rate=t.percent(b.rate), basis=t.amount(b.basis)),
                "tax": t.amount(b.tax),
                "allowance": t.amount(b.allowance) if b.allowance else "",
            }
            for b in totals.breakdown
        ],
        "totals": {
            "line_total": t.amount(totals.line_total),
            "tax_basis_total": t.amount(totals.tax_basis_total),
            "grand_total": t.money(totals.grand_total),
        },
        "grand_label": t("grand_total") if show_rates else t("invoice_total"),
        "tax_notes": [note for note in document.tax_note.split("\n") if note],
        "deductions": [
            {
                "label": t("less_partial", number=d.source.number, date=t.date(d.source.issue_date)),
                "detail": t(
                    "deduction_detail",
                    net=t.amount(d.net_amount),
                    rate=t.percent(d.tax_rate),
                    tax=t.amount(d.tax_amount),
                ),
                "gross": t.amount(d.gross_amount),
            }
            for d in deductions
        ],
        "due_total": t.money(due),
        "due_label": t("credit_due") if doc_type == Type.CANCELLATION else t("amount_due"),
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
