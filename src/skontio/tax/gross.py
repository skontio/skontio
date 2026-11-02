"""Bruttopreise eingeben (B2C) – EN 16931 rechnet immer netto → brutto.

For an entered gross unit price we look for the net unit price (4 decimals, BT-146) whose line,
calculated exactly like ``engine.calculate`` (net rounded per line, tax on the net), gives the
wanted gross amount. Some gross amounts are not reachable at a given rate (e.g. 9,99 € at 19 %);
then the nearest reachable amount is used (on a tie the lower one) and reported, never silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from skontio.tax.engine import money

HUNDRED = Decimal("100")
PRICE = Decimal("0.0001")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class GrossResult:
    unit_price: Decimal  # net, 4 decimals
    target: Decimal  # wanted gross of the line
    reached: Decimal  # gross of the line with that net price

    @property
    def exact(self) -> bool:
        return self.target == self.reached


def _line(quantity: Decimal, price: Decimal, discount: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """(net, gross) of one line, rounded as in engine.calculate (tax per line as if alone)."""
    net = money(quantity * price) - money(quantity * price * discount / HUNDRED)
    return net, net + money(net * rate / HUNDRED)


def net_unit_price(
    gross_unit: Decimal, quantity: Decimal, rate: Decimal, discount: Decimal = Decimal("0")
) -> GrossResult:
    if gross_unit < 0 or quantity < 0:
        raise ValueError("Bruttopreis und Menge dürfen nicht negativ sein")
    target = money(gross_unit * quantity * (1 - discount / HUNDRED))
    if quantity == 0:
        price = (gross_unit / (1 + rate / HUNDRED)).quantize(PRICE, rounding=ROUND_HALF_UP)
        return GrossResult(price, target, Decimal("0.00"))
    base = (gross_unit / (1 + rate / HUNDRED)).quantize(PRICE, rounding=ROUND_HALF_UP)
    best: tuple[tuple, Decimal, Decimal] | None = None  # (rank, price, gross)
    # a cent of net moves the price by 0.01 / quantity; search that neighbourhood in 0.0001 steps
    span = min(max(int((CENT / quantity / PRICE).to_integral_value()) * 3, 3), 3000)  # bounded work
    for step in range(-span, span + 1):
        price = base + step * PRICE
        if price < 0:
            continue
        _, gross = _line(quantity, price, discount, rate)
        # closest first; on a tie the lower amount (never charge more than announced), then the nearest price
        rank = (abs(gross - target), gross > target, abs(price - base))
        if best is None or rank < best[0]:
            best = (rank, price, gross)
    assert best is not None
    return GrossResult(best[1], target, best[2])
