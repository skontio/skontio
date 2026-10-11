"""Tax engine: abstract tax treatment → EN 16931 category/rate, and document totals.

Pure functions without Django so that UI, MCP tools, the assistant and the e-invoice
writer all compute identical numbers. Calculation rules follow EN 16931:

* BR-CO-10  sum of line net amounts
* BR-CO-13  tax basis = line total − document allowances
* BR-CO-17  VAT per category = category basis × rate, rounded to 2 decimals
            (never the sum of rounded per-line taxes)
"""

from __future__ import annotations

import datetime as dt
import enum
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from skontio.tax import de

CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class TaxError(ValueError):
    pass


class Regime(enum.StrEnum):
    SMALL_BUSINESS = "small_business"  # Kleinunternehmer, § 19 UStG
    STANDARD = "standard"  # Regelbesteuerung


class TaxTreatment(enum.StrEnum):
    """What a catalog item *is*; the concrete rate depends on regime and service date."""

    STANDARD = "standard"  # Regelsatz
    REDUCED = "reduced"  # ermäßigter Satz
    ZERO = "zero"  # Nullsatz (§ 12 Abs. 3 UStG)
    EXEMPT = "exempt"  # steuerfrei nach § 4 UStG
    REVERSE_CHARGE = "reverse_charge"  # § 13b UStG / EU-B2B-Dienstleistung
    INTRA_EU_SUPPLY = "intra_eu_supply"  # innergemeinschaftliche Lieferung
    EXPORT = "export"  # Ausfuhrlieferung
    NOT_TAXABLE = "not_taxable"  # nicht steuerbar


@dataclass(frozen=True)
class ResolvedTax:
    category: str  # UNCL5305 code as used by EN 16931
    rate: Decimal
    exemption_reason: str = ""


def resolve(treatment: TaxTreatment, regime: Regime, on_date: dt.date) -> ResolvedTax:
    """Resolve the abstract treatment for the service date (Leistungsdatum)."""
    if regime is Regime.SMALL_BUSINESS:
        if treatment is TaxTreatment.NOT_TAXABLE:
            return ResolvedTax("O", Decimal("0"), de.NOT_TAXABLE_NOTE)
        if treatment in (TaxTreatment.REVERSE_CHARGE, TaxTreatment.INTRA_EU_SUPPLY):
            raise TaxError(
                "Kleinunternehmer können keine Reverse-Charge- oder innergemeinschaftlichen Lieferungen abrechnen"
            )
        return ResolvedTax("E", Decimal("0"), de.SMALL_BUSINESS_NOTE)

    match treatment:
        case TaxTreatment.STANDARD | TaxTreatment.REDUCED:
            table = de.STANDARD_RATES if treatment is TaxTreatment.STANDARD else de.REDUCED_RATES
            rate = de.rate_on(table, on_date)
            if rate is None:
                raise TaxError(f"Kein Steuersatz für {on_date.isoformat()} hinterlegt")
            return ResolvedTax("S", rate)
        case TaxTreatment.ZERO:
            if on_date < de.ZERO_RATE_FROM:
                raise TaxError("Der Nullsteuersatz gilt erst ab 01.01.2023")
            return ResolvedTax("Z", Decimal("0"))
        case TaxTreatment.EXEMPT:
            return ResolvedTax("E", Decimal("0"), de.EXEMPT_NOTE)
        case TaxTreatment.REVERSE_CHARGE:
            return ResolvedTax("AE", Decimal("0"), de.REVERSE_CHARGE_NOTE)
        case TaxTreatment.INTRA_EU_SUPPLY:
            return ResolvedTax("K", Decimal("0"), de.INTRA_EU_SUPPLY_NOTE)
        case TaxTreatment.EXPORT:
            return ResolvedTax("G", Decimal("0"), de.EXPORT_NOTE)
        case TaxTreatment.NOT_TAXABLE:
            return ResolvedTax("O", Decimal("0"), de.NOT_TAXABLE_NOTE)
    raise TaxError(f"Unbekannte Steuerbehandlung {treatment!r}")


@dataclass(frozen=True)
class LineInput:
    quantity: Decimal
    unit_price: Decimal
    tax: ResolvedTax
    discount_percent: Decimal = Decimal("0")


@dataclass(frozen=True)
class LineResult:
    gross_amount: Decimal  # quantity × unit price
    discount_amount: Decimal
    net_amount: Decimal  # BT-131
    tax: ResolvedTax


@dataclass(frozen=True)
class TaxBreakdown:  # BG-23
    category: str
    rate: Decimal
    exemption_reason: str
    line_total: Decimal
    allowance: Decimal
    basis: Decimal  # BT-116
    tax: Decimal  # BT-117


@dataclass(frozen=True)
class Totals:
    lines: list[LineResult]
    breakdown: list[TaxBreakdown]
    line_total: Decimal  # BT-106
    allowance_total: Decimal  # BT-107
    tax_basis_total: Decimal  # BT-109
    tax_total: Decimal  # BT-110
    grand_total: Decimal  # BT-112


def _check_percent(value: Decimal, what: str) -> None:
    if not (Decimal("0") <= value <= HUNDRED):
        raise TaxError(f"{what} muss zwischen 0 und 100 % liegen")


def calculate(lines: list[LineInput], *, document_discount_percent: Decimal = Decimal("0")) -> Totals:
    if not lines:
        raise TaxError("Ein Dokument braucht mindestens eine Position")
    _check_percent(document_discount_percent, "Der Dokumentrabatt")

    results: list[LineResult] = []
    groups: dict[tuple[str, Decimal, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for line in lines:
        if line.quantity < 0 or line.unit_price < 0:
            raise TaxError("Menge und Einzelpreis dürfen nicht negativ sein (Korrekturen über Gutschrift/Storno)")
        _check_percent(line.discount_percent, "Der Positionsrabatt")
        gross = money(line.quantity * line.unit_price)
        discount = money(line.quantity * line.unit_price * line.discount_percent / HUNDRED)
        net = gross - discount
        results.append(LineResult(gross, discount, net, line.tax))
        groups[(line.tax.category, line.tax.rate, line.tax.exemption_reason)] += net

    breakdown = []
    for (category, rate, reason), line_total in sorted(groups.items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        allowance = money(line_total * document_discount_percent / HUNDRED)
        basis = line_total - allowance
        breakdown.append(
            TaxBreakdown(category, rate, reason, line_total, allowance, basis, money(basis * rate / HUNDRED))
        )

    line_total = sum((r.net_amount for r in results), Decimal("0.00"))
    allowance_total = sum((b.allowance for b in breakdown), Decimal("0.00"))
    tax_total = sum((b.tax for b in breakdown), Decimal("0.00"))
    basis_total = line_total - allowance_total
    return Totals(
        lines=results,
        breakdown=breakdown,
        line_total=money(line_total),
        allowance_total=money(allowance_total),
        tax_basis_total=money(basis_total),
        tax_total=money(tax_total),
        grand_total=money(basis_total + tax_total),
    )
