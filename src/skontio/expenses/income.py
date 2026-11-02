"""Sonstige Einnahmen ohne Rechnung: entry, cancellation and the tax view used by the reports.

Tax follows the confirmed status on the day the money came in (Istversteuerung):
* Kleinunternehmer: no VAT may be shown – the rate must be 0, the amount is gross income.
* Regelbesteuerung: 19 % / 7 % are split into net and VAT; 0 % is tax-free or not taxable.
Whether an income counts towards the small-business limit (§ 19 Abs. 2 UStG) depends on its kind:
sales of fixed assets are excluded by law; refunds and genuine subsidies are no consideration.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from skontio.attachments.services import safe_filename
from skontio.audit.services import record
from skontio.expenses.models import Expense, OtherIncome
from skontio.expenses.services import _store_receipt
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import allocate_number
from skontio.tax.engine import Regime
from skontio.taxstatus.services import TaxStatusError, regime_for

SEQUENCE = "other_income"
PATTERN = "EI-{YYYY}-{####}"
CENT = Decimal("0.01")
RATES = (Decimal("0"), Decimal("7"), Decimal("19"))
NOT_TURNOVER = {OtherIncome.Category.ASSET_SALE, OtherIncome.Category.REFUND, OtherIncome.Category.SUBSIDY}
FIELDS = {"received_on", "payer", "description", "category", "gross_amount", "vat_rate", "method", "note"}
MAX_AMOUNT = Decimal("9999999999.99")


class IncomeError(ValueError):
    pass


@dataclass(frozen=True)
class IncomeTax:
    regime: Regime
    net: Decimal
    vat: Decimal


def regime_on(company, day: dt.date) -> Regime:
    try:
        return regime_for(company, day)
    except TaxStatusError:
        return Regime.SMALL_BUSINESS


def split(income: OtherIncome, regime: Regime) -> IncomeTax:
    gross = Decimal(income.gross_amount).quantize(CENT)
    if regime is Regime.SMALL_BUSINESS or not income.vat_rate:
        return IncomeTax(regime, gross, Decimal("0.00"))
    net = (gross * 100 / (100 + Decimal(income.vat_rate))).quantize(CENT, rounding=ROUND_HALF_UP)
    return IncomeTax(regime, net, gross - net)


def counts_as_turnover(income: OtherIncome) -> bool:
    return income.category not in NOT_TURNOVER


def _validate(income: OtherIncome) -> None:
    income.payer = " ".join((income.payer or "").split())[:200]
    if not income.payer:
        raise IncomeError("Bitte angeben, von wem das Geld kam.")
    if income.received_on is None:
        raise IncomeError("Bitte das Datum des Zahlungseingangs angeben.")
    if income.received_on > timezone.localdate() + dt.timedelta(days=1):
        raise IncomeError("Das Datum liegt in der Zukunft.")
    try:
        amount = Decimal(income.gross_amount)
    except (TypeError, ArithmeticError) as exc:
        raise IncomeError("Der Betrag ist ungültig.") from exc
    if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
        raise IncomeError("Der Betrag muss größer als 0 und kleiner als 10 Mrd. € sein.")
    if amount != amount.quantize(CENT):
        raise IncomeError("Der Betrag hat mehr als zwei Nachkommastellen.")
    if Decimal(income.vat_rate) not in RATES:
        raise IncomeError("USt-Satz bitte 0, 7 oder 19 %.")
    if income.category not in OtherIncome.Category.values:
        raise IncomeError("Unbekannte Art.")
    if income.method not in Expense.Method.values:
        raise IncomeError("Unbekannte Zahlart.")
    if Decimal(income.vat_rate) and regime_on(income.company, income.received_on) is Regime.SMALL_BUSINESS:
        raise IncomeError("Als Kleinunternehmer (§ 19 UStG) darf keine Umsatzsteuer enthalten sein – Satz 0 % wählen.")


def create_income(company, *, user=None, receipt: bytes | None = None, receipt_filename: str = "", **fields):
    unknown = set(fields) - FIELDS
    if unknown:
        raise IncomeError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    income = OtherIncome(company=company, created_by=user, **fields)
    _validate(income)
    with transaction.atomic():
        if receipt:
            try:
                income.receipt = _store_receipt(receipt, receipt_filename, income.received_on)
            except ValueError as exc:
                raise IncomeError(str(exc)) from exc
            income.receipt_filename = safe_filename(receipt_filename or "beleg")
        NumberSequence.objects.get_or_create(company=company, doc_type=SEQUENCE, defaults={"pattern": PATTERN})
        income.number = allocate_number(company, SEQUENCE, timezone.localdate())
        income.save()
        record(
            "other_income.create", actor=user, company=company, entity="other_income", entity_id=income.pk,
            data={"number": income.number, "gross": str(income.gross_amount), "vat_rate": str(income.vat_rate)},
        )  # fmt: skip
    return income


def cancel_income(income: OtherIncome, *, reason: str, user=None) -> OtherIncome:
    if income.cancelled_at:
        raise IncomeError(f"Einnahme {income.number} ist bereits storniert.")
    if not reason.strip():
        raise IncomeError("Bitte einen Grund angeben.")
    with transaction.atomic():
        income.cancelled_at = timezone.now()
        income.cancel_reason = reason.strip()[:300]
        income.save(update_fields=["cancelled_at", "cancel_reason"])
        record(
            "other_income.cancel", actor=user, company=income.company, entity="other_income",
            entity_id=income.pk, data={"reason": income.cancel_reason},
        )  # fmt: skip
    return income


def active(company, start: dt.date, end: dt.date):
    return OtherIncome.objects.filter(
        company=company, received_on__range=(start, end), cancelled_at__isnull=True
    ).order_by("received_on", "pk")
