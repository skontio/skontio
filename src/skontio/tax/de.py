"""Country pack Germany: VAT rates with validity periods and invoice notes.

Sources (to be re-verified on every rate change, see docs/legal/sources.md):
* § 12 Abs. 1/2 UStG – 19 % / 7 % since 01.01.2007
* Zweites Corona-Steuerhilfegesetz – 16 % / 5 % from 01.07.2020 to 31.12.2020
* § 12 Abs. 3 UStG (JStG 2022) – 0 % for certain photovoltaic supplies since 01.01.2023
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

# (valid_from, valid_to_inclusive or None, rate)
STANDARD_RATES = [
    (dt.date(2007, 1, 1), dt.date(2020, 6, 30), Decimal("19")),
    (dt.date(2020, 7, 1), dt.date(2020, 12, 31), Decimal("16")),
    (dt.date(2021, 1, 1), None, Decimal("19")),
]
REDUCED_RATES = [
    (dt.date(2007, 1, 1), dt.date(2020, 6, 30), Decimal("7")),
    (dt.date(2020, 7, 1), dt.date(2020, 12, 31), Decimal("5")),
    (dt.date(2021, 1, 1), None, Decimal("7")),
]
ZERO_RATE_FROM = dt.date(2023, 1, 1)

SMALL_BUSINESS_NOTE = "Steuerbefreiung für Kleinunternehmer gemäß § 19 UStG – es wird keine Umsatzsteuer berechnet."
EXEMPT_NOTE = "Steuerfreie Leistung gemäß § 4 UStG."
REVERSE_CHARGE_NOTE = "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge, § 13b UStG)."
INTRA_EU_SUPPLY_NOTE = "Steuerfreie innergemeinschaftliche Lieferung (§ 4 Nr. 1 Buchst. b, § 6a UStG)."
EXPORT_NOTE = "Steuerfreie Ausfuhrlieferung (§ 4 Nr. 1 Buchst. a, § 6 UStG)."
NOT_TAXABLE_NOTE = "Im Inland nicht steuerbare Leistung."


def rate_on(table: list[tuple[dt.date, dt.date | None, Decimal]], on_date: dt.date) -> Decimal | None:
    for valid_from, valid_to, rate in table:
        if valid_from <= on_date and (valid_to is None or on_date <= valid_to):
            return rate
    return None
