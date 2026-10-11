"""Syntax-neutral EN 16931 invoice data handed from the document module to the XML writers.

Amounts are ``Decimal``; rounding happens here (BR-DEC: 2 decimals for amounts) so that
CII, UBL and the PDF all show identical numbers.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Party:
    name: str
    street: str
    postcode: str
    city: str
    country: str
    email: str | None = None
    phone: str | None = None
    contact_name: str | None = None
    vat_id: str | None = None  # BT-31
    tax_number: str | None = None  # BT-32
    identifier: str | None = None  # BT-29 Seller identifier


@dataclass(frozen=True)
class InvoiceLine:
    name: str
    quantity: Decimal
    unit_code: str  # UN/ECE Rec 20, e.g. C62 (Stück), HUR (Stunde)
    unit_price: Decimal

    @property
    def net_amount(self) -> Decimal:
        return money(self.quantity * self.unit_price)


@dataclass(frozen=True)
class PaymentInstructions:
    iban: str
    bic: str | None = None
    account_name: str | None = None
    means_code: str = "58"  # SEPA credit transfer


@dataclass(frozen=True)
class Invoice:
    number: str
    issue_date: dt.date
    seller: Party
    buyer: Party
    lines: list[InvoiceLine]
    tax_category: str  # EN 16931 / UNCL5305: S, Z, E, AE, K, G, O
    tax_rate: Decimal
    payment: PaymentInstructions
    buyer_reference: str
    service_date: dt.date | None = None
    due_date: dt.date | None = None
    payment_terms: str | None = None
    tax_exemption_reason: str | None = None
    type_code: str = "380"  # commercial invoice
    currency: str = "EUR"
    notes: list[str] = field(default_factory=list)

    @property
    def line_total(self) -> Decimal:
        return money(sum((line.net_amount for line in self.lines), Decimal("0")))

    @property
    def tax_total(self) -> Decimal:
        # BR-CO-17: VAT is calculated on the category total, not per line
        return money(self.line_total * self.tax_rate / Decimal("100"))

    @property
    def grand_total(self) -> Decimal:
        return self.line_total + self.tax_total
