"""Positionsgruppen: a heading over consecutive lines with the same group and a subtotal after them.

Display only – taxes, totals and the e-invoice stay per line (EN 16931 has no line groups).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

MAX_LENGTH = 200


def clean(group: str) -> str:
    group = " ".join((group or "").split())
    if len(group) > MAX_LENGTH:
        raise ValueError(f"Der Gruppenname darf höchstens {MAX_LENGTH} Zeichen lang sein")
    return group


def annotate(rows: Sequence[dict], group_of: Callable[[int], str], net_of: Callable[[int], Decimal | None]) -> None:
    """Set ``heading`` (group title where a group starts) and ``subtotal`` ((title, net) where it ends) on each row.

    ``subtotal`` net is None when any line of the group has no amount (e.g. totals could not be calculated).
    """
    count = len(rows)
    start = 0
    for index in range(count):
        group = group_of(index)
        row: dict[str, Any] = rows[index]
        row["heading"] = group if group and (index == 0 or group_of(index - 1) != group) else ""
        if row["heading"]:
            start = index
        ends = group and (index == count - 1 or group_of(index + 1) != group)
        if ends:
            nets = [net_of(i) for i in range(start, index + 1)]
            total = None if any(n is None for n in nets) else sum(nets, Decimal("0"))
            row["subtotal"] = (group, total)
        else:
            row["subtotal"] = None
