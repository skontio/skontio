"""Umsatzsteuer-Voranmeldung (§ 18 UStG): Kennzahlen for one month or quarter, to be typed into Mein ELSTER.

* Output tax by Istversteuerung: payments received in the period on invoices under Regelbesteuerung,
  split over the invoice's tax breakdown in proportion to the payment.
* Input tax (Kz 66) by receipt date of expenses under Regelbesteuerung (Vorsteuer needs the invoice).
* § 13b (Kz 46/47 for services from EU suppliers, 84/85 otherwise) by receipt date – also for
  Kleinunternehmer, who owe this tax but may not deduct it (no Kz 67).
* Sollversteuerung is not supported yet (``SollNotSupported``) rather than computed wrongly.
Assessment bases (Bemessungsgrundlagen) are whole euros, taxes have cents, as the form expects.
The Kennzahl numbers are kept per form (see docs/legal/sources.md – to be verified every year).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from skontio.documents.services import stored_totals
from skontio.expenses.models import Expense
from skontio.expenses.services import input_vat, net_amount, reverse_charge_vat
from skontio.payments.models import Payment
from skontio.recurring.services import add_months
from skontio.tax.engine import Regime
from skontio.taxstatus.models import VatReturn
from skontio.taxstatus.services import TaxStatusError, regime_for

CENT = Decimal("0.01")
ZERO = Decimal("0")
EU = set("AT BE BG CY CZ DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split())  # without DE

KENNZAHLEN = {
    "81": "Steuerpflichtige Umsätze zu 19 % (Bemessungsgrundlage)",
    "86": "Steuerpflichtige Umsätze zu 7 % (Bemessungsgrundlage)",
    "35": "Umsätze zu anderen Steuersätzen (Bemessungsgrundlage)",
    "36": "Steuer auf Umsätze zu anderen Steuersätzen",
    "41": "Innergemeinschaftliche Lieferungen an Abnehmer mit USt-IdNr.",
    "43": "Weitere steuerfreie Umsätze mit Vorsteuerabzug (z. B. Ausfuhrlieferungen)",
    "48": "Steuerfreie Umsätze ohne Vorsteuerabzug",
    "21": "Nicht steuerbare sonstige Leistungen im übrigen Gemeinschaftsgebiet (§ 18b)",
    "45": "Übrige nicht steuerbare Umsätze",
    "46": "§ 13b: Sonstige Leistungen von Unternehmern aus dem übrigen Gemeinschaftsgebiet",
    "47": "§ 13b: Steuer zu Kz 46",
    "84": "§ 13b: Andere Leistungen (Bemessungsgrundlage)",
    "85": "§ 13b: Steuer zu Kz 84",
    "66": "Vorsteuerbeträge aus Rechnungen anderer Unternehmer",
    "67": "Vorsteuerbeträge aus Leistungen im Sinne des § 13b",
    "83": "Verbleibende Umsatzsteuer-Vorauszahlung bzw. Überschuss (−)",
}
BASES = {"81", "86", "35", "41", "43", "48", "21", "45", "46", "84"}  # whole euros


class SollNotSupported(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    year: int
    code: str  # "M01".."M12", "Q1".."Q4"

    @property
    def start(self) -> dt.date:
        if self.code.startswith("Q"):
            return dt.date(self.year, (int(self.code[1]) - 1) * 3 + 1, 1)
        return dt.date(self.year, int(self.code[1:]), 1)

    @property
    def end(self) -> dt.date:
        months = 3 if self.code.startswith("Q") else 1
        return add_months(self.start, months) - dt.timedelta(days=1)

    @property
    def label(self) -> str:
        if self.code.startswith("Q"):
            return f"{self.code[1]}. Quartal {self.year}"
        return f"{self.start:%m}/{self.year}"

    def deadline(self, extension: bool) -> dt.date:
        """10th day after the period (§ 18 Abs. 1 UStG), one month later with Dauerfristverlängerung."""
        day = add_months(self.end + dt.timedelta(days=1), 1 if extension else 0) + dt.timedelta(days=9)
        while day.weekday() >= 5:  # § 108 Abs. 3 AO: next working day (public holidays not considered)
            day += dt.timedelta(days=1)
        return day


def periods(year: int, kind: str) -> list[Period]:
    codes = [f"Q{q}" for q in range(1, 5)] if kind == "quarterly" else [f"M{m:02d}" for m in range(1, 13)]
    return [Period(year, code) for code in codes]


def parse_period(year: int, code: str) -> Period:
    if code not in {p.code for kind in ("quarterly", "monthly") for p in periods(year, kind)}:
        raise ValueError(f"Unbekannter Zeitraum: {code}")
    return Period(year, code)


@dataclass
class VatReport:
    period: Period
    values: dict[str, Decimal]
    small_business: bool
    notes: list[str] = field(default_factory=list)

    def rows(self) -> list[tuple[str, str, Decimal]]:
        return [(kz, label, self.values[kz]) for kz, label in KENNZAHLEN.items() if self.values.get(kz)]

    def frozen(self) -> dict[str, str]:
        return {kz: str(value) for kz, value in self.values.items() if value}


def _add(values: dict, kz: str, amount: Decimal) -> None:
    values[kz] = values.get(kz, ZERO) + amount


def _outputs(company, period: Period, values: dict) -> None:
    payments = Payment.objects.filter(
        company=company, received_on__gte=period.start, received_on__lte=period.end
    ).select_related("document", "document__customer")
    for payment in payments:
        doc = payment.document
        if doc.regime != Regime.STANDARD or not doc.gross_total:
            continue
        share = payment.amount / doc.gross_total
        for subtotal in stored_totals(doc).breakdown:
            basis, tax = subtotal.basis * share, subtotal.tax * share
            if subtotal.category == "S" and subtotal.rate == 19:
                _add(values, "81", basis)
            elif subtotal.category == "S" and subtotal.rate == 7:
                _add(values, "86", basis)
            elif subtotal.category == "S":
                _add(values, "35", basis)
                _add(values, "36", tax)
            elif subtotal.category == "K":
                _add(values, "41", basis)
            elif subtotal.category == "G":
                _add(values, "43", basis)
            elif subtotal.category == "E":
                _add(values, "48", basis)
            elif subtotal.category == "AE":
                _add(values, "21" if doc.customer and doc.customer.country in EU else "45", basis)
            else:  # O (not taxable in Germany) and Z
                _add(values, "45", basis)


def _regime_on(company, day: dt.date) -> Regime:
    try:
        return regime_for(company, day)
    except TaxStatusError:
        return Regime.SMALL_BUSINESS


def _inputs(company, period: Period, values: dict) -> None:
    expenses = Expense.objects.filter(
        company=company, cancelled_at__isnull=True, receipt_date__gte=period.start, receipt_date__lte=period.end
    )
    for expense in expenses:
        standard = _regime_on(company, expense.receipt_date) is Regime.STANDARD
        if not expense.reverse_charge:
            if standard:
                _add(values, "66", input_vat(expense))
            continue
        eu = expense.supplier_country in EU
        _add(values, "46" if eu else "84", net_amount(expense))
        _add(values, "47" if eu else "85", reverse_charge_vat(expense))
        if standard:
            _add(values, "67", reverse_charge_vat(expense))


def vat_report(company, period: Period) -> VatReport:
    if company.vat_basis == "soll":
        raise SollNotSupported(
            "Sollversteuerung wird noch nicht unterstützt – die Kennzahlen bitte mit der Buchhaltung ermitteln."
        )
    values: dict[str, Decimal] = {}
    _outputs(company, period, values)
    _inputs(company, period, values)
    rounded = {
        kz: amount.quantize(Decimal("1"), rounding=ROUND_DOWN)
        if kz in BASES
        else amount.quantize(CENT, rounding=ROUND_HALF_UP)
        for kz, amount in values.items()
    }
    tax = (
        rounded.get("81", ZERO) * Decimal("0.19")
        + rounded.get("86", ZERO) * Decimal("0.07")
        + rounded.get("36", ZERO)
        + rounded.get("47", ZERO)
        + rounded.get("85", ZERO)
    )
    rounded["83"] = (tax - rounded.get("66", ZERO) - rounded.get("67", ZERO)).quantize(CENT, rounding=ROUND_HALF_UP)
    small = _regime_on(company, period.end) is Regime.SMALL_BUSINESS
    report = VatReport(period, rounded, small)
    if small:
        report.notes.append(
            "Kleinunternehmer: Eine Voranmeldung ist nur für Steuer nach § 13b nötig; Vorsteuer ist nicht abziehbar."
        )
    report.notes.append(
        "Keine Steuerberatung. Die Kennzahlen bitte mit dem aktuellen Formular in Mein ELSTER abgleichen; "
        "Bemessungsgrundlagen stehen in vollen Euro, die Steuer darauf berechnet ELSTER."
    )
    return report


def submitted(company, period: Period) -> VatReturn | None:
    return VatReturn.objects.filter(company=company, year=period.year, period=period.code).first()


def differences(report: VatReport, vat_return: VatReturn) -> dict[str, tuple[str, str]]:
    """Kennzahlen that changed since the period was marked as submitted (→ berichtigte Anmeldung)."""
    now = report.frozen()
    return {
        kz: (vat_return.values.get(kz, "0"), now.get(kz, "0"))
        for kz in sorted(set(now) | set(vat_return.values))
        if Decimal(vat_return.values.get(kz, "0")) != Decimal(now.get(kz, "0"))
    }
