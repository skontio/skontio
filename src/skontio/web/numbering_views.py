"""Einstellungen → Nummernkreise: Muster, Rücksetzung und nächste Nummer je Dokumentart."""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from skontio.crm.models import Customer
from skontio.crm.services import (
    CUSTOMER_SEQUENCE,
    DEFAULT_CUSTOMER_PATTERN,
    DEFAULT_SUPPLIER_PATTERN,
    SUPPLIER_SEQUENCE,
)
from skontio.documents.models import Document
from skontio.documents.services import DEFAULT_PATTERNS, SEQUENCE_TYPE
from skontio.dunning import services as dunning
from skontio.dunning.models import DunningNotice
from skontio.expenses import income as other_income
from skontio.expenses import services as expenses
from skontio.expenses.models import Expense, OtherIncome
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import NumberingError, format_number, update_sequence
from skontio.web.support import admin_required, current_company

Type = Document.Type
YEARLY, NEVER = NumberSequence.Reset.YEARLY, NumberSequence.Reset.NEVER


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    pattern: str
    reset: str
    max_length: int

    def existing(self, company) -> list[str]:
        if self.key in (CUSTOMER_SEQUENCE, SUPPLIER_SEQUENCE):
            qs = Customer.objects.filter(company=company)
        elif self.key == expenses.SEQUENCE:
            qs = Expense.objects.filter(company=company)
        elif self.key == other_income.SEQUENCE:
            qs = OtherIncome.objects.filter(company=company)
        elif self.key == dunning.SEQUENCE:
            qs = DunningNotice.objects.filter(company=company)
        else:
            types = [t for t, seq in SEQUENCE_TYPE.items() if seq == self.key] + [self.key]
            qs = Document.objects.filter(company=company, doc_type__in=types, number__isnull=False)
        return list(qs.values_list("number", flat=True))


KINDS = [
    Kind(Type.INVOICE, "Rechnungen (auch Abschlag und Schluss)", DEFAULT_PATTERNS[Type.INVOICE], YEARLY, 64),
    Kind(Type.CANCELLATION, "Stornorechnungen", DEFAULT_PATTERNS[Type.CANCELLATION], YEARLY, 64),
    Kind(Type.QUOTE, "Angebote", DEFAULT_PATTERNS[Type.QUOTE], YEARLY, 64),
    Kind(Type.ORDER_CONFIRMATION, "Auftragsbestätigungen", DEFAULT_PATTERNS[Type.ORDER_CONFIRMATION], YEARLY, 64),
    Kind(CUSTOMER_SEQUENCE, "Kundennummern", DEFAULT_CUSTOMER_PATTERN, NEVER, 32),
    Kind(SUPPLIER_SEQUENCE, "Lieferantennummern", DEFAULT_SUPPLIER_PATTERN, NEVER, 32),
    Kind(expenses.SEQUENCE, "Ausgaben", expenses.PATTERN, YEARLY, 32),
    Kind(other_income.SEQUENCE, "Sonstige Einnahmen", other_income.PATTERN, YEARLY, 32),
    Kind(dunning.SEQUENCE, "Mahnungen", dunning.PATTERN, YEARLY, 32),
]


def _sequence(company, kind: Kind) -> NumberSequence:
    found = NumberSequence.objects.filter(company=company, doc_type=kind.key).first()
    return found or NumberSequence(company=company, doc_type=kind.key, pattern=kind.pattern, reset=kind.reset)


def _next_value(seq: NumberSequence, today) -> int:
    """The value the next allocation will use (after a yearly reset it starts at 1 again)."""
    if seq.reset == YEARLY and seq.period_year is not None and seq.period_year < today.year:
        return 1
    return seq.next_value


@admin_required
def numbering(request):
    company = current_company()
    if company is None:
        return redirect("setup")
    today = timezone.localdate()
    errors: dict[str, str] = {}
    if request.method == "POST":
        kind = next((k for k in KINDS if k.key == request.POST.get("kind")), None)
        if kind is not None:
            try:
                next_value = int(request.POST.get("next_value", ""))
            except ValueError:
                errors[kind.key] = "Die nächste Nummer muss eine ganze Zahl sein."
            else:
                try:
                    update_sequence(
                        _sequence(company, kind), pattern=request.POST.get("pattern", "")[:64],
                        reset=request.POST.get("reset", ""), next_value=next_value,
                        existing=kind.existing(company), today=today, user=request.user, max_length=kind.max_length,
                    )  # fmt: skip
                except NumberingError as exc:
                    errors[kind.key] = str(exc)
                else:
                    messages.success(request, f"Nummernkreis „{kind.label}“ gespeichert.")
                    return redirect("numbering")
    rows = []
    for kind in KINDS:
        seq = _sequence(company, kind)
        value = _next_value(seq, today)
        try:
            preview = format_number(seq.pattern, value, today)
        except NumberingError:
            preview = "–"
        rows.append({"kind": kind, "seq": seq, "next_value": value, "preview": preview, "error": errors.get(kind.key)})
    return render(request, "web/numbering.html", {"rows": rows, "resets": NumberSequence.Reset.choices})
