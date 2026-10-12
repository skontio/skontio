"""Korrektur-Assistent (plan §6.1): invoices whose VAT treatment no longer matches the confirmed tax
status for their date of supply – typically because the 100,000 € limit was crossed retroactively
(Regelbesteuerung from the transaction that crosses it) or a status change was confirmed late.

A correction never edits a finalized invoice. It creates a Stornorechnung (mirroring the original)
and a new invoice *draft* under today's status that refers to the original. The person reviews and
finalizes the draft; payments are then moved by counter-booking (GoBD), never by editing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from django.db import transaction

from skontio.audit.services import record
from skontio.documents.languages import Lang
from skontio.documents.models import INVOICE_TYPES, Document
from skontio.documents.services import DocumentError, add_line, cancel_invoice, create_draft, supply_regime
from skontio.payments.models import Payment
from skontio.payments.services import open_amount, paid_amount, record_payment, reverse_payment
from skontio.tax.engine import Regime, TaxError, TaxTreatment, resolve

Type = Document.Type
CORRECTABLE = {Type.INVOICE, Type.PARTIAL_INVOICE}
PRICE_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class Candidate:
    document: Document
    recorded: Regime
    current: Regime
    paid: Decimal

    @property
    def correctable(self) -> bool:
        return self.document.doc_type in CORRECTABLE


def candidates(company) -> list[Candidate]:
    """Finalized, not cancelled invoices whose recorded regime differs from today's confirmed one."""
    invoices = (
        Document.objects.filter(company=company, doc_type__in=INVOICE_TYPES, status=Document.Status.FINALIZED)
        .exclude(children__doc_type=Type.CANCELLATION)
        .select_related("customer")
        .order_by("issue_date", "number")
    )
    found = []
    for invoice in invoices:
        try:
            current = supply_regime(invoice)
        except DocumentError:
            continue  # no confirmed status for that date – nothing to compare with
        if current != Regime(invoice.regime):
            found.append(Candidate(invoice, Regime(invoice.regime), current, paid_amount(invoice)))
    return found


def _tax_date(document: Document) -> dt.date:
    return document.service_period_end or document.service_date or document.issue_date


def correct_invoice(
    invoice: Document,
    *,
    keep: Literal["net", "gross"],
    issue_date: dt.date,
    created_by=None,
) -> Document:
    """Cancel ``invoice`` and return a new draft under the current tax status.

    ``keep="net"``: prices stay, the customer owes the VAT on top. ``keep="gross"``: the gross amount
    stays (VAT is taken out of the previous amount); line totals may differ by a cent through rounding.
    """
    invoice = Document.objects.select_related("company", "customer").get(pk=invoice.pk)
    if invoice.doc_type not in CORRECTABLE:
        raise DocumentError(
            "Schlussrechnungen bitte von Hand korrigieren: zuerst die Schlussrechnung stornieren, "
            "dann die Abschlagsrechnungen korrigieren und eine neue Schlussrechnung erstellen"
        )
    if keep not in ("net", "gross"):
        raise DocumentError("keep muss net oder gross sein")
    current = supply_regime(invoice)
    if current == Regime(invoice.regime):
        raise DocumentError(f"Rechnung {invoice.number} passt bereits zum Steuerstatus")
    t = Lang(invoice.language)
    with transaction.atomic():
        storno = cancel_invoice(invoice, issue_date=issue_date, reason=t("correction_reason"), created_by=created_by)
        reference = t("replaces", number=invoice.number, date=t.date(invoice.issue_date), storno=storno.number)
        draft = create_draft(
            invoice.company,
            Type(invoice.doc_type),
            invoice.customer,
            issue_date=issue_date,
            service_date=invoice.service_date,
            service_period=(
                (invoice.service_period_start, invoice.service_period_end) if invoice.service_period_start else None
            ),
            created_by=created_by,
            language=invoice.language,
            intro_text="\n\n".join(text for text in (reference, invoice.intro_text) if text),
            closing_text=invoice.closing_text,
            document_discount_percent=invoice.document_discount_percent,
            parent=invoice.parent,
            replaces=invoice,
            template=invoice.template,
        )
        for line in invoice.lines.all():
            price = line.unit_price
            if keep == "gross":
                try:
                    new_rate = resolve(TaxTreatment(line.tax_treatment), current, _tax_date(invoice)).rate
                except TaxError as exc:
                    raise DocumentError(str(exc)) from exc
                price = (price * (100 + line.tax_rate) / (100 + new_rate)).quantize(PRICE_PLACES)
            add_line(
                draft,
                text=line.text,
                description=line.description,
                quantity=line.quantity,
                unit_code=line.unit_code,
                unit_price=price,
                tax_treatment=TaxTreatment(line.tax_treatment),
                discount_percent=line.discount_percent,
                item=line.item,
            )
        record(
            "document.correction",
            actor=created_by,
            company=invoice.company,
            entity="document",
            entity_id=invoice.pk,
            data={"storno": storno.pk, "draft": draft.pk, "keep": keep, "from": invoice.regime, "to": str(current)},
        )
    return draft


def transferable_payments(replacement: Document) -> list[Payment]:
    """Payments booked on the replaced invoice that were not reversed yet."""
    if replacement.replaces_id is None:
        return []
    payments = replacement.replaces.payments.filter(reverses__isnull=True, reversed_by__isnull=True)
    return list(payments.order_by("received_on", "pk"))


def transfer_payments(replacement: Document, *, created_by=None) -> tuple[Decimal, Decimal]:
    """Move payments from the replaced invoice to its finalized replacement by counter-booking.

    Returns (moved, left). A payment larger than what is still open on the replacement stays on the
    old invoice (e.g. an overpayment after the gross amount went down) – settle that one by hand.
    """
    if replacement.status != Document.Status.FINALIZED:
        raise DocumentError("Zahlungen erst übernehmen, wenn die neue Rechnung festgeschrieben ist")
    old = replacement.replaces
    moved = left = Decimal("0")
    with transaction.atomic():
        for payment in transferable_payments(replacement):
            if payment.amount > open_amount(replacement):
                left += payment.amount
                continue
            reason = f"Übertrag auf {replacement.number} (Rechnungskorrektur)"
            reverse_payment(payment, reason=reason, on=payment.received_on, created_by=created_by)
            record_payment(
                replacement,
                payment.amount,
                payment.received_on,
                method=payment.method,
                note=f"Übertrag von {old.number}",
                created_by=created_by,
            )
            moved += payment.amount
        record(
            "payments.transfer",
            actor=created_by,
            company=replacement.company,
            entity="document",
            entity_id=replacement.pk,
            data={"from": old.pk, "moved": str(moved), "left": str(left)},
        )
    return moved, left
