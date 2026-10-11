from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from skontio.documents.models import Document
from skontio.payments.models import Payment, PaymentError, PaymentLocked

__all__ = [
    "PaymentError",
    "PaymentLocked",
    "open_amount",
    "paid_amount",
    "payment_status",
    "record_payment",
    "reverse_payment",
]

ZERO = Decimal("0.00")


def paid_amount(document: Document) -> Decimal:
    return document.payments.aggregate(total=Sum("amount"))["total"] or ZERO


def open_amount(document: Document) -> Decimal:
    return (document.gross_total or ZERO) - paid_amount(document)


def payment_status(document: Document) -> str:
    paid = paid_amount(document)
    if paid <= 0:
        return "open"
    return "paid" if paid >= (document.gross_total or ZERO) else "partial"


def record_payment(
    document: Document,
    amount: Decimal,
    received_on: dt.date,
    *,
    method: Payment.Method = Payment.Method.BANK_TRANSFER,
    note: str = "",
    created_by=None,
) -> Payment:
    if amount <= 0:
        raise PaymentError("Der Zahlungsbetrag muss positiv sein")
    with transaction.atomic():
        doc = Document.objects.select_for_update().get(pk=document.pk)
        if doc.doc_type != Document.Type.INVOICE or doc.status != Document.Status.FINALIZED:
            raise PaymentError("Zahlungen können nur auf festgeschriebene Rechnungen gebucht werden")
        if doc.children.filter(doc_type=Document.Type.CANCELLATION).exists():
            raise PaymentError(f"Rechnung {doc.number} ist storniert")
        if amount > open_amount(doc):
            raise PaymentError(f"Der Betrag übersteigt den offenen Betrag von {open_amount(doc)} €")
        return Payment.objects.create(
            company=doc.company,
            document=doc,
            amount=amount,
            received_on=received_on,
            method=method,
            note=note,
            created_by=created_by,
        )


def reverse_payment(payment: Payment, *, reason: str, on: dt.date, created_by=None) -> Payment:
    """GoBD: a booking is corrected by a counter-booking, the original stays visible."""
    if not reason.strip():
        raise PaymentError("Für eine Stornobuchung ist ein Grund anzugeben")
    with transaction.atomic():
        original = Payment.objects.select_for_update().get(pk=payment.pk)
        if original.reverses_id is not None:
            raise PaymentError("Eine Stornobuchung kann nicht storniert werden")
        if Payment.objects.filter(reverses=original).exists():
            raise PaymentError("Diese Zahlung ist bereits storniert")
        return Payment.objects.create(
            company=original.company,
            document=original.document,
            amount=-original.amount,
            received_on=on,
            method=original.method,
            note=f"Storno: {reason.strip()}",
            reverses=original,
            created_by=created_by,
        )
