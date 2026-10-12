"""Bankumsätze: import without duplicates, suggest matches, book with one confirmation.

Suggestions only – a payment is recorded (or an expense marked paid) when a person confirms.
Incoming money is matched to open invoices by invoice number in the purpose (strong), by exact
open amount, and by customer name; outgoing money to unpaid expenses with the same amount.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from skontio.audit.services import record
from skontio.banking.models import BankTransaction
from skontio.banking.parsers import Row, StatementError, parse_statement
from skontio.documents.models import INVOICE_TYPES, Document
from skontio.expenses.models import Expense
from skontio.expenses.services import update_expense
from skontio.payments.models import Payment
from skontio.payments.services import open_amount, record_payment

MAX_BYTES = 10 * 1024 * 1024


class BankingError(ValueError):
    pass


def _key(row: Row, occurrence: int) -> str:
    raw = "|".join([row.booked_on.isoformat(), str(row.amount), row.currency, row.counterparty, row.iban, row.purpose])
    return hashlib.sha256(f"{raw}|{occurrence}".encode()).hexdigest()


def import_statement(company, data: bytes, *, user=None) -> tuple[int, int]:
    """Return (new, already known). Identical lines inside one file are kept apart by their count."""
    if len(data) > MAX_BYTES:
        raise BankingError("Die Datei ist größer als 10 MB.")
    try:
        source, rows = parse_statement(data)
    except StatementError as exc:
        raise BankingError(str(exc)) from exc
    seen: Counter = Counter()
    created = known = 0
    with transaction.atomic():
        existing = set(BankTransaction.objects.filter(company=company).values_list("import_key", flat=True))
        for row in rows:
            identity = (row.booked_on, row.amount, row.currency, row.counterparty, row.iban, row.purpose)
            seen[identity] += 1
            key = _key(row, seen[identity])
            if key in existing:
                known += 1
                continue
            BankTransaction.objects.create(
                company=company,
                booked_on=row.booked_on,
                amount=row.amount,
                currency=row.currency,
                counterparty=row.counterparty,
                iban=row.iban,
                purpose=row.purpose,
                import_key=key,
                source=source,
                imported_by=user,
            )
            existing.add(key)
            created += 1
        record("bank.import", actor=user, company=company, data={"source": source, "new": created, "known": known})
    return created, known


@dataclass(frozen=True)
class Suggestion:
    target: Document | Expense
    amount: Decimal
    reason: str
    score: int


def _compact(value: str) -> str:
    return re.sub(r"[\s\-_/]", "", value).upper()


def suggestions(tx: BankTransaction, limit: int = 3) -> list[Suggestion]:
    if tx.done:
        return []
    if tx.amount > 0:
        purpose, counterparty = _compact(tx.purpose), tx.counterparty.lower()
        found = []
        invoices = (
            Document.objects.filter(company=tx.company, doc_type__in=INVOICE_TYPES, status=Document.Status.FINALIZED)
            .exclude(children__doc_type=Document.Type.CANCELLATION)
            .select_related("customer")
        )
        for invoice in invoices:
            due = open_amount(invoice)
            if due <= 0:
                continue
            score, reasons = 0, []
            if invoice.number and _compact(invoice.number) in purpose:
                score += 10
                reasons.append("Rechnungsnummer im Verwendungszweck")
            if due == tx.amount:
                score += 5
                reasons.append("Betrag passt genau")
            if invoice.customer and invoice.customer.name.lower() in counterparty:
                score += 2
                reasons.append("Kundenname")
            if score >= 5 and tx.amount <= due:
                found.append(Suggestion(invoice, due, ", ".join(reasons), score))
        return sorted(found, key=lambda s: -s.score)[:limit]
    expenses = Expense.objects.filter(
        company=tx.company, paid_on__isnull=True, cancelled_at__isnull=True, gross_amount=-tx.amount
    ).order_by("receipt_date")
    return [Suggestion(e, e.gross_amount, "Betrag passt, noch unbezahlt", 5) for e in expenses[:limit]]


def book_payment(tx: BankTransaction, invoice: Document, *, user=None) -> Payment:
    if tx.done:
        raise BankingError("Dieser Umsatz ist bereits zugeordnet.")
    if tx.amount <= 0 or invoice.company_id != tx.company_id:
        raise BankingError("Nur Zahlungseingänge lassen sich einer Rechnung zuordnen.")
    with transaction.atomic():
        payment = record_payment(
            invoice,
            tx.amount,
            tx.booked_on,
            method=Payment.Method.BANK_TRANSFER,
            note=f"Bankumsatz: {tx.counterparty} {tx.purpose}"[:300],
            created_by=user,
        )
        tx.payment = payment
        tx.save(update_fields=["payment"])
        record(
            "bank.match",
            actor=user,
            company=tx.company,
            entity="document",
            entity_id=invoice.pk,
            data={"transaction": tx.pk, "amount": str(tx.amount)},
        )
    return payment


def link_expense(tx: BankTransaction, expense: Expense, *, user=None) -> Expense:
    if tx.done:
        raise BankingError("Dieser Umsatz ist bereits zugeordnet.")
    if tx.amount >= 0 or expense.company_id != tx.company_id:
        raise BankingError("Nur Abbuchungen lassen sich einer Ausgabe zuordnen.")
    with transaction.atomic():
        if expense.paid_on is None:
            update_expense(expense, user=user, paid_on=tx.booked_on)
        tx.expense = expense
        tx.save(update_fields=["expense"])
        record(
            "bank.match",
            actor=user,
            company=tx.company,
            entity="expense",
            entity_id=expense.pk,
            data={"transaction": tx.pk, "amount": str(tx.amount)},
        )
    return expense


def ignore(tx: BankTransaction, *, user=None) -> None:
    """Private or irrelevant line (e.g. own transfer) – kept, but no longer offered for matching."""
    tx.ignored = not tx.ignored
    tx.save(update_fields=["ignored"])
    record(
        "bank.ignore" if tx.ignored else "bank.unignore",
        actor=user,
        company=tx.company,
        data={"transaction": tx.pk},
    )
