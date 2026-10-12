"""Finalized ``Document`` → syntax-neutral EN 16931 ``Invoice``.

Only the snapshot is used (party snapshots, per-line tax snapshot), never current master
data, so a re-rendered document is identical to the one that was sent.
"""

from __future__ import annotations

from decimal import Decimal

from skontio.documents.models import INVOICE_TYPES, Document, PartySnapshot
from skontio.einvoice.model import Invoice, LineData, Party, PaymentInstructions, PrecedingInvoice, build_invoice
from skontio.tax.engine import ResolvedTax

TYPE_CODES = {
    Document.Type.INVOICE: "380",
    Document.Type.PARTIAL_INVOICE: "326",  # Partial invoice (XRechnung BR-DE-17 allows it)
    Document.Type.FINAL_INVOICE: "380",
    Document.Type.CANCELLATION: "381",
}


def deduction_notes(document: Document) -> list[str]:
    """§ 14 Abs. 5 Satz 2 UStG: deducted payments with their tax amounts, per partial invoice and rate."""
    notes = []
    for deduction in document.deductions.select_related("source"):
        source = deduction.source
        notes.append(
            f"Abzüglich Abschlagsrechnung {source.number} vom {source.issue_date:%d.%m.%Y}: "
            f"Entgelt {deduction.net_amount} EUR, Umsatzsteuer {deduction.tax_rate.normalize():f} % "
            f"{deduction.tax_amount} EUR"
        )
    return notes


class SnapshotMismatch(RuntimeError):
    """Recomputed totals differ from the stored snapshot – never render such a document."""


def _seller(data: dict) -> Party:
    return Party(
        name=data["name"],
        street=data["street"],
        postcode=data["postcode"],
        city=data["city"],
        country=data["country"],
        email=data["email"] or None,
        phone=data["phone"] or None,
        contact_name=data["contact_name"] or None,
        vat_id=data["vat_id"] or None,
        tax_number=data["tax_number"] or None,
        identifier=data["seller_identifier"] or None,
    )


def _buyer(data: dict) -> Party:
    return Party(
        name=data["name"],
        street=data["street"],
        postcode=data["postcode"],
        city=data["city"],
        country=data["country"],
        email=data["email"] or None,
        vat_id=data["vat_id"] or None,
    )


def invoice_from_document(document: Document) -> Invoice:
    if document.doc_type not in TYPE_CODES or document.status != Document.Status.FINALIZED:
        raise ValueError("Nur festgeschriebene Rechnungen und Stornorechnungen werden zur E-Rechnung")
    snapshots = {s.role: s.data for s in document.party_snapshots.all()}
    seller, buyer = snapshots[PartySnapshot.Role.SELLER], snapshots[PartySnapshot.Role.BUYER]
    if seller.get("redacted") or buyer.get("redacted"):
        raise ValueError("Die Parteidaten dieses Dokuments wurden nach Ablauf der Aufbewahrungsfrist geschwärzt")

    lines = [
        LineData(
            name=line.text,
            description=line.description,
            quantity=line.quantity,
            unit_code=line.unit_code,
            unit_price=line.unit_price,
            discount_percent=line.discount_percent,
            tax=ResolvedTax(line.tax_category, line.tax_rate, line.tax_exemption_reason),
        )
        for line in document.lines.all()
    ]
    period = (
        (document.service_period_start, document.service_period_end)
        if document.service_period_start and document.service_period_end
        else None
    )
    preceding = None
    notes = []
    if document.doc_type == Document.Type.CANCELLATION:
        parent = document.parent
        preceding = PrecedingInvoice(parent.number, parent.issue_date)
        notes.append(f"Stornierung der Rechnung {parent.number} vom {parent.issue_date:%d.%m.%Y}")
        if document.cancellation_reason:
            notes.append(f"Grund: {document.cancellation_reason}")
        payment_terms = "Der Rechnungsbetrag wird gutgeschrieben bzw. erstattet."
    else:
        if document.doc_type == Document.Type.PARTIAL_INVOICE and document.parent:
            notes.append(f"Abschlagsrechnung zum Angebot {document.parent.number}")
        payment_terms = document.payment_terms_text or (
            f"Zahlbar ohne Abzug bis {document.due_date:%d.%m.%Y}." if document.due_date else ""
        )

    invoice = build_invoice(
        number=document.number,
        issue_date=document.issue_date,
        type_code=TYPE_CODES[Document.Type(document.doc_type)],
        seller=_seller(seller),
        buyer=_buyer(buyer),
        buyer_reference=buyer.get("buyer_reference", ""),
        lines=lines,
        document_discount_percent=document.document_discount_percent,
        service_date=document.service_date,
        service_period=period,
        due_date=document.due_date if document.doc_type in INVOICE_TYPES else None,
        payment_terms=payment_terms or None,
        payment=PaymentInstructions(
            iban=seller["iban"], bic=seller["bic"] or None, account_name=seller["account_holder"] or None
        ),
        currency=document.currency,
        notes=notes + deduction_notes(document),
        preceding_invoice=preceding,
        prepaid_amount=sum((d.gross_amount for d in document.deductions.all()), Decimal("0.00")),
    )
    stored = (document.net_total, document.tax_total, document.gross_total)
    if (invoice.tax_basis_total, invoice.tax_total, invoice.grand_total) != stored:
        raise SnapshotMismatch(f"Summen von {document.number} weichen vom Snapshot ab")
    return invoice
