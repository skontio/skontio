"""Arbeitskosten im Rechnungsbetrag (§ 35a EStG): what a private customer may claim as labour costs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


@dataclass(frozen=True)
class LaborCosts:
    net: Decimal
    vat: Decimal

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat


def compute(lines, line_results, document_discount_percent) -> LaborCosts:
    """``lines`` carry ``labor_percent``; ``line_results`` the matching tax results (net_amount, tax.rate)."""
    keep = 1 - Decimal(document_discount_percent or 0) / 100
    net = vat = Decimal("0")
    for line, result in zip(lines, line_results, strict=True):
        share = result.net_amount * Decimal(line.labor_percent or 0) / 100 * keep
        net += share
        vat += share * Decimal(result.tax.rate or 0) / 100
    return LaborCosts(net.quantize(CENT, rounding=ROUND_HALF_UP), vat.quantize(CENT, rounding=ROUND_HALF_UP))
