"""Ausgaben: capture with receipt, change with audit trail, cancel instead of delete (GoBD).

``gross_amount`` is what was paid. With ``reverse_charge`` (§ 13b UStG, e.g. advertising from
Ireland) the supplier invoices without VAT; the buyer owes the VAT to the tax office – a
Kleinunternehmer too. Skontio points that out but does not file anything.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from skontio.archive.services import release, retention_end, store
from skontio.attachments.services import MAX_BYTES, AttachmentError, detect_mime, safe_filename
from skontio.audit.services import record
from skontio.documents.renditions import INVOICE_RETENTION_YEARS, de_amount
from skontio.expenses.models import Expense, PendingReceipt
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import allocate_number
from skontio.tax.engine import Regime

CENT = Decimal("0.01")
SEQUENCE = "expense"
PATTERN = "AU-{YYYY}-{####}"
RECEIPT_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp", "application/xml"}
GWG_LIMIT = Decimal("800")  # § 6 Abs. 2 EStG, net
HOSPITALITY_SHARE = Decimal("0.70")  # § 4 Abs. 5 Nr. 2 EStG
EDITABLE = {
    "receipt_date",
    "paid_on",
    "supplier",
    "supplier_country",
    "description",
    "category",
    "gross_amount",
    "vat_rate",
    "reverse_charge",
    "method",
    "note",
}


class ExpenseError(ValueError):
    pass


def net_amount(expense: Expense) -> Decimal:
    if expense.reverse_charge:
        return Decimal(expense.gross_amount)
    return (Decimal(expense.gross_amount) * 100 / (100 + Decimal(expense.vat_rate))).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def input_vat(expense: Expense) -> Decimal:
    return Decimal("0.00") if expense.reverse_charge else Decimal(expense.gross_amount) - net_amount(expense)


def reverse_charge_vat(expense: Expense) -> Decimal:
    if not expense.reverse_charge:
        return Decimal("0.00")
    return (Decimal(expense.gross_amount) * Decimal(expense.vat_rate) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def hints(expense: Expense, regime: Regime | None) -> list[str]:
    """Readable warnings shown with the expense (no tax advice, pointers only)."""
    found = []
    if not expense.receipt_id:
        found.append("Beleg fehlt – ohne Beleg erkennt das Finanzamt die Ausgabe meist nicht an.")
    if expense.reverse_charge and regime is Regime.SMALL_BUSINESS:
        found.append(
            f"§ 13b UStG: Auch als Kleinunternehmer schuldest du hierfür {de_amount(reverse_charge_vat(expense))} € "
            "Umsatzsteuer und musst sie beim Finanzamt anmelden."
        )
    elif expense.reverse_charge:
        found.append(
            f"§ 13b UStG: {de_amount(reverse_charge_vat(expense))} € Umsatzsteuer anmelden; "
            "bei Regelbesteuerung ist sie zugleich als Vorsteuer abziehbar."
        )
    elif expense.supplier_country != "DE" and Decimal(expense.vat_rate) == 0:
        found.append(
            "Ausländischer Lieferant ohne Umsatzsteuer: Häufig greift § 13b UStG (Steuerschuldnerschaft) – "
            "bitte prüfen und ggf. ankreuzen."
        )
    if expense.category == Expense.Category.GWG and net_amount(expense) > GWG_LIMIT:
        found.append(
            "Über 800 € netto: Das ist kein GWG, sondern ein Anlagegut, das über die Nutzungsdauer abgeschrieben wird."
        )
    if expense.category == Expense.Category.HOSPITALITY:
        found.append("Bewirtung: nur 70 % abziehbar; auf dem Beleg Anlass und Teilnehmer vermerken.")
    return found


def _store_receipt(data: bytes, filename: str, receipt_date: dt.date | None):
    if len(data) > MAX_BYTES:
        raise ExpenseError("Der Beleg darf höchstens 20 MB groß sein.")
    try:
        mime = detect_mime(data, filename)
    except AttachmentError as exc:
        raise ExpenseError(str(exc)) from exc
    if mime not in RECEIPT_TYPES:
        raise ExpenseError("Beleg bitte als PDF, Foto (JPEG, PNG, WebP) oder E-Rechnung (XML) hochladen.")
    retention = retention_end(receipt_date, INVOICE_RETENTION_YEARS) if receipt_date else None
    return store(data, mime=mime, retention_until=retention)


def _validate(expense: Expense) -> None:
    expense.supplier = (expense.supplier or "").strip()
    expense.supplier_country = (expense.supplier_country or "DE").strip().upper()
    if not expense.supplier:
        raise ExpenseError("Bitte den Lieferanten angeben.")
    if len(expense.supplier_country) != 2 or not expense.supplier_country.isalpha():
        raise ExpenseError("Land bitte als zweistelligen Code angeben (z. B. DE, IE).")
    if expense.gross_amount is None or Decimal(expense.gross_amount) <= 0:
        raise ExpenseError("Der Betrag muss größer als 0 sein.")
    if not Decimal("0") <= Decimal(expense.vat_rate) <= Decimal("30"):
        raise ExpenseError("Der USt-Satz muss zwischen 0 und 30 % liegen.")
    if expense.category not in Expense.Category.values:
        raise ExpenseError("Unbekannte Kategorie.")
    if expense.method not in Expense.Method.values:
        raise ExpenseError("Unbekannte Zahlart.")
    if expense.paid_on and expense.paid_on > timezone.localdate() + dt.timedelta(days=1):
        raise ExpenseError("Das Zahlungsdatum liegt in der Zukunft.")


def create_expense(
    company,
    *,
    user=None,
    receipt: bytes | None = None,
    receipt_filename: str = "",
    pending: PendingReceipt | None = None,
    **fields,
):
    """``pending``: a receipt from the Belegeingang that becomes this expense's receipt."""
    unknown = set(fields) - EDITABLE
    if unknown:
        raise ExpenseError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    expense = Expense(company=company, created_by=user, **fields)
    _validate(expense)
    with transaction.atomic():
        if receipt:
            expense.receipt = _store_receipt(receipt, receipt_filename, expense.receipt_date)
            expense.receipt_filename = safe_filename(receipt_filename or "beleg")
        elif pending is not None:
            pending = PendingReceipt.objects.select_for_update().get(pk=pending.pk, company=company)
            if pending.expense_id:
                raise ExpenseError("Dieser Beleg ist bereits einer Ausgabe zugeordnet.")
            expense.receipt, expense.receipt_filename = pending.blob, pending.filename
            _keep_for(pending.blob, expense.receipt_date)
        NumberSequence.objects.get_or_create(company=company, doc_type=SEQUENCE, defaults={"pattern": PATTERN})
        expense.number = allocate_number(company, SEQUENCE, timezone.localdate())  # numbered by entry date
        expense.save()
        if pending is not None:
            pending.expense = expense
            pending.save(update_fields=["expense"])
        record(
            "expense.create",
            actor=user,
            company=company,
            entity="expense",
            entity_id=expense.pk,
            data={"number": expense.number, "gross": str(expense.gross_amount), "receipt": bool(expense.receipt_id)},
        )
    return expense


def update_expense(
    expense: Expense, *, user=None, receipt: bytes | None = None, receipt_filename: str = "", **changes
) -> Expense:
    if expense.cancelled_at:
        raise ExpenseError(f"Ausgabe {expense.number} ist storniert.")
    unknown = set(changes) - EDITABLE
    if unknown:
        raise ExpenseError(f"Nicht änderbar: {', '.join(sorted(unknown))}")
    before = {field: getattr(expense, field) for field in changes}
    for field, value in changes.items():
        setattr(expense, field, value)
    _validate(expense)
    diff = {
        f: [str(before[f]), str(getattr(expense, f))] for f in changes if str(before[f]) != str(getattr(expense, f))
    }
    with transaction.atomic():
        if receipt:
            old = expense.receipt_id
            expense.receipt = _store_receipt(receipt, receipt_filename, expense.receipt_date)
            expense.receipt_filename = safe_filename(receipt_filename or "beleg")
            diff["receipt"] = [str(old or ""), str(expense.receipt_id)]  # the old receipt stays archived
        if not diff:
            return expense
        expense.save()
        record(
            "expense.update",
            actor=user,
            company=expense.company,
            entity="expense",
            entity_id=expense.pk,
            data={"changed": diff},
        )
    return expense


def _keep_for(blob, receipt_date: dt.date) -> None:
    until = retention_end(receipt_date, INVOICE_RETENTION_YEARS)
    if blob.retention_until is None or blob.retention_until < until:
        blob.retention_until = until
        blob.save(update_fields=["retention_until"])


def add_pending(company, data: bytes, *, filename: str, note: str = "", user=None) -> PendingReceipt:
    """Belegeingang: keep the file until it is turned into an expense (no retention yet – it may be junk)."""
    if not data:
        raise ExpenseError("Die Datei ist leer.")
    with transaction.atomic():
        pending = PendingReceipt.objects.create(
            company=company,
            blob=_store_receipt(data, filename, None),
            filename=safe_filename(filename or "beleg"),
            note=note.strip()[:500],
            uploaded_by=user,
        )
        record("receipt.upload", actor=user, company=company, entity="pending_receipt", entity_id=pending.pk)
    return pending


def discard_pending(pending: PendingReceipt, *, user=None) -> None:
    if pending.expense_id:
        raise ExpenseError("Der Beleg gehört schon zu einer Ausgabe.")
    with transaction.atomic():
        blob = pending.blob
        record(
            "receipt.discard",
            actor=user,
            company=pending.company,
            entity="pending_receipt",
            entity_id=pending.pk,
            data={"filename": pending.filename},
        )
        pending.delete()
        release(blob)  # stays if the same file is used elsewhere or under retention


def cancel_expense(expense: Expense, *, reason: str, user=None) -> Expense:
    if expense.cancelled_at:
        raise ExpenseError(f"Ausgabe {expense.number} ist bereits storniert.")
    if not reason.strip():
        raise ExpenseError("Bitte einen Grund angeben.")
    with transaction.atomic():
        expense.cancelled_at = timezone.now()
        expense.cancel_reason = reason.strip()[:300]
        expense.save(update_fields=["cancelled_at", "cancel_reason"])
        record(
            "expense.cancel",
            actor=user,
            company=expense.company,
            entity="expense",
            entity_id=expense.pk,
            data={"reason": expense.cancel_reason},
        )
    return expense
