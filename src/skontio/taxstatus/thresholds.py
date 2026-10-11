"""Monitoring of the small-business limits (§ 19 Abs. 1 UStG).

Gesamtumsatz is computed from *received* payments (vereinnahmte Entgelte) in the calendar
year, net of VAT. Skontio only warns and proposes – a person confirms any regime change.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from skontio.payments.models import Payment

CENT = Decimal("0.01")


@dataclass(frozen=True)
class ThresholdRule:
    valid_from: dt.date
    previous_year_limit: Decimal
    current_year_limit: Decimal
    source: str
    verified_on: dt.date


# Versioned: add a new rule when the law changes; never edit a past one.
SMALL_BUSINESS_RULES = [
    ThresholdRule(
        valid_from=dt.date(2025, 1, 1),
        previous_year_limit=Decimal("25000"),
        current_year_limit=Decimal("100000"),
        source="https://www.gesetze-im-internet.de/ustg_1980/__19.html",
        verified_on=dt.date(2026, 10, 11),
    ),
]


class Level(enum.Enum):
    OK = "ok"
    WARN_80 = "warn_80"
    WARN_90 = "warn_90"
    EXCEEDED = "exceeded"


@dataclass(frozen=True)
class SmallBusinessStatus:
    year: int
    revenue: Decimal
    previous_year_revenue: Decimal
    current_limit: Decimal
    previous_year_limit: Decimal
    level: Level
    crossed_on: dt.date | None
    previous_year_exceeded: bool
    rule: ThresholdRule

    @property
    def percent(self) -> Decimal:
        return (self.revenue / self.current_limit * 100).quantize(Decimal("0.1"))


def rule_for(year: int) -> ThresholdRule:
    applicable = [r for r in SMALL_BUSINESS_RULES if r.valid_from <= dt.date(year, 12, 31)]
    if not applicable:
        raise ValueError(f"Für {year} sind keine Kleinunternehmergrenzen hinterlegt")
    return max(applicable, key=lambda r: r.valid_from)


def _net_receipts(company, year: int) -> list[tuple[dt.date, Decimal]]:
    payments = (
        Payment.objects.filter(company=company, received_on__year=year)
        .select_related("document")
        .order_by("received_on", "pk")
    )
    receipts = []
    for payment in payments:
        gross, net = payment.document.gross_total, payment.document.net_total
        share = payment.amount if not gross else payment.amount * net / gross
        receipts.append((payment.received_on, share.quantize(CENT, rounding=ROUND_HALF_UP)))
    return receipts


def _level(revenue: Decimal, limit: Decimal) -> Level:
    if revenue > limit:
        return Level.EXCEEDED
    if revenue >= limit * Decimal("0.9"):
        return Level.WARN_90
    if revenue >= limit * Decimal("0.8"):
        return Level.WARN_80
    return Level.OK


def small_business_status(company, today: dt.date) -> SmallBusinessStatus:
    rule = rule_for(today.year)
    receipts = _net_receipts(company, today.year)
    revenue, crossed_on = Decimal("0.00"), None
    for received_on, amount in receipts:
        revenue += amount
        if crossed_on is None and revenue > rule.current_year_limit:
            crossed_on = received_on
    if revenue <= rule.current_year_limit:
        crossed_on = None  # a later reversal may bring the total back below the limit
    previous = sum((amount for _, amount in _net_receipts(company, today.year - 1)), Decimal("0.00"))
    return SmallBusinessStatus(
        year=today.year,
        revenue=revenue,
        previous_year_revenue=previous,
        current_limit=rule.current_year_limit,
        previous_year_limit=rule.previous_year_limit,
        level=_level(revenue, rule.current_year_limit),
        crossed_on=crossed_on,
        previous_year_exceeded=previous > rule.previous_year_limit,
        rule=rule,
    )


def would_exceed(company, net_amount: Decimal, on: dt.date) -> bool:
    """Preview: would receiving this (net) amount on ``on`` cross the current-year limit?"""
    status = small_business_status(company, on)
    return status.revenue + net_amount > status.current_limit
