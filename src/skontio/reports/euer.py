"""Einnahmenüberschussrechnung (§ 4 Abs. 3 EStG) from payments and expenses – a preparation for the
Anlage EÜR, not a filing. Zufluss-/Abflussprinzip: income counts when paid, expenses when paid.

* Kleinunternehmer: everything gross (no input tax deduction).
* Regelbesteuerung: net amounts; collected VAT is income, paid input VAT is an expense.
* Each payment follows the tax status recorded on its invoice; each expense the confirmed status on
  its receipt date – so a change in the middle of the year is split correctly.
Not yet covered (stated in ``notes``): depreciation (AfA), VAT payments to/refunds from the tax
office, the 10-day rule for recurring payments, private use.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from skontio.expenses.models import Expense
from skontio.expenses.services import HOSPITALITY_SHARE, input_vat, net_amount, reverse_charge_vat
from skontio.payments.models import Payment
from skontio.tax.engine import Regime
from skontio.taxstatus.services import TaxStatusError, regime_for

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
INCOME_LABELS = {
    "small_business": "Betriebseinnahmen als Kleinunternehmer (§ 19 UStG)",
    "taxable": "Umsatzsteuerpflichtige Betriebseinnahmen (netto)",
    "tax_free": "Umsatzsteuerfreie und nicht steuerbare Betriebseinnahmen",
    "collected_vat": "Vereinnahmte Umsatzsteuer",
}
INPUT_VAT_LABEL = "Gezahlte Vorsteuerbeträge"


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Line:
    key: str
    label: str
    amount: Decimal


@dataclass
class EuerReport:
    year: int
    income: list[Line]
    expenses: list[Line]
    non_deductible: Decimal
    reverse_charge_vat: Decimal
    unpaid_expenses: int
    notes: list[str] = field(default_factory=list)

    @property
    def income_total(self) -> Decimal:
        return sum((line.amount for line in self.income), ZERO)

    @property
    def expense_total(self) -> Decimal:
        return sum((line.amount for line in self.expenses), ZERO)

    @property
    def profit(self) -> Decimal:
        return self.income_total - self.expense_total


@dataclass(frozen=True)
class Entry:
    """One row of the EÜR journal (CSV export)."""

    date: dt.date
    kind: str  # "Einnahme" | "Ausgabe"
    number: str
    counterparty: str
    line: str
    net: Decimal
    vat: Decimal
    gross: Decimal
    deductible: Decimal
    regime: str


def _payment_entries(company, year: int) -> list[Entry]:
    entries = []
    payments = (
        Payment.objects.filter(company=company, received_on__year=year)
        .select_related("document", "document__customer")
        .order_by("received_on", "pk")
    )
    for payment in payments:
        doc, gross = payment.document, _money(payment.amount)
        gross_total, net_total = doc.gross_total or ZERO, doc.net_total or ZERO
        if doc.regime == Regime.SMALL_BUSINESS:
            key, net, vat = "small_business", gross, ZERO
        else:
            net = _money(payment.amount * net_total / gross_total) if gross_total else gross
            vat = gross - net
            key = "taxable" if doc.tax_total else "tax_free"
        entries.append(
            Entry(
                date=payment.received_on,
                kind="Einnahme",
                number=doc.number,
                counterparty=doc.customer.name if doc.customer else "",
                line=key,
                net=net,
                vat=vat,
                gross=gross,
                deductible=ZERO,
                regime=str(doc.regime),
            )
        )
    return entries


def _regime_on(company, day: dt.date, cache: dict) -> Regime:
    if day not in cache:
        try:
            cache[day] = regime_for(company, day)
        except TaxStatusError:
            cache[day] = Regime.SMALL_BUSINESS  # no status recorded: no input tax deduction
    return cache[day]


def _expense_entries(company, year: int) -> list[Entry]:
    entries, cache = [], {}
    expenses = Expense.objects.filter(company=company, paid_on__year=year, cancelled_at__isnull=True).order_by(
        "paid_on", "pk"
    )
    for expense in expenses:
        regime = _regime_on(company, expense.receipt_date, cache)
        net, gross = net_amount(expense), _money(expense.gross_amount)
        base = gross if regime is Regime.SMALL_BUSINESS else net
        deductible = _money(base * HOSPITALITY_SHARE) if expense.category == Expense.Category.HOSPITALITY else base
        entries.append(
            Entry(
                date=expense.paid_on,
                kind="Ausgabe",
                number=expense.number,
                counterparty=expense.supplier,
                line=expense.category,
                net=net,
                vat=input_vat(expense) if regime is Regime.STANDARD else ZERO,
                gross=gross,
                deductible=deductible,
                regime=str(regime),
            )
        )
    return entries


def journal(company, year: int) -> list[Entry]:
    entries = _payment_entries(company, year) + _expense_entries(company, year)
    return sorted(entries, key=lambda e: (e.date, e.kind, e.number))


def euer(company, year: int) -> EuerReport:
    income: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for entry in _payment_entries(company, year):
        income[entry.line] += entry.net
        income["collected_vat"] += entry.vat
    expenses: dict[str, Decimal] = defaultdict(lambda: ZERO)
    non_deductible = ZERO
    for entry in _expense_entries(company, year):
        expenses[entry.line] += entry.deductible
        expenses["input_vat"] += entry.vat
        if entry.line == Expense.Category.HOSPITALITY:
            base = entry.gross if entry.regime == Regime.SMALL_BUSINESS else entry.net
            non_deductible += base - entry.deductible
    labels = dict(Expense.Category.choices) | {"input_vat": INPUT_VAT_LABEL}
    rc = Expense.objects.filter(company=company, paid_on__year=year, cancelled_at__isnull=True, reverse_charge=True)
    report = EuerReport(
        year=year,
        income=[Line(k, label, income[k]) for k, label in INCOME_LABELS.items() if income.get(k)],
        expenses=[Line(k, labels[k], expenses[k]) for k in [*Expense.Category.values, "input_vat"] if expenses.get(k)],
        non_deductible=non_deductible,
        reverse_charge_vat=sum((reverse_charge_vat(e) for e in rc), ZERO),
        unpaid_expenses=Expense.objects.filter(
            company=company, receipt_date__year=year, paid_on__isnull=True, cancelled_at__isnull=True
        ).count(),
    )
    report.notes = _notes(report)
    return report


def _notes(report: EuerReport) -> list[str]:
    notes = [
        "Vorbereitung für die Anlage EÜR, keine Steuerberatung. Die Zeilennummern des Formulars ändern sich "
        "jährlich – Beträge beim Übertragen der passenden Zeile zuordnen.",
        "Noch nicht enthalten: Abschreibungen (AfA), Zahlungen an und Erstattungen vom Finanzamt, "
        "die 10-Tage-Regel für regelmäßig wiederkehrende Zahlungen und Privatanteile.",
    ]
    if report.non_deductible:
        notes.append(f"Nicht abziehbarer Anteil der Bewirtung (30 %): {report.non_deductible} €.")
    if report.reverse_charge_vat:
        notes.append(
            f"Umsatzsteuer nach § 13b UStG aus Ausgaben: {report.reverse_charge_vat} € – beim Finanzamt anzumelden; "
            "gezahlt ist sie Betriebsausgabe (bei Regelbesteuerung zugleich Vorsteuer)."
        )
    if report.unpaid_expenses:
        notes.append(
            f"{report.unpaid_expenses} Ausgabe(n) mit Belegdatum {report.year} ohne Zahlungsdatum – zählen erst, "
            "wenn sie bezahlt sind."
        )
    return notes
