"""Syntax-neutral EN 16931 invoice data handed from the document module to the XML writers.

All amounts come from the tax engine (``skontio.tax.engine.calculate``) so CII, UBL, the PDF
and the database show identical numbers. ``build_invoice`` is the only constructor callers use.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from skontio.tax.engine import LineInput, ResolvedTax, calculate


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
    vat_id: str | None = None  # BT-31 / BT-48
    tax_number: str | None = None  # BT-32 (seller only)
    identifier: str | None = None  # BT-29 Seller identifier


@dataclass(frozen=True)
class PaymentInstructions:
    iban: str
    bic: str | None = None
    account_name: str | None = None
    means_code: str = "58"  # SEPA credit transfer


@dataclass(frozen=True)
class PrecedingInvoice:  # BG-3, mandatory for cancellations/credit notes
    number: str
    issue_date: dt.date


@dataclass(frozen=True)
class LineData:
    """Input line: what the document says before calculation."""

    name: str
    quantity: Decimal
    unit_code: str  # UN/ECE Rec 20, e.g. C62 (Stück), HUR (Stunde)
    unit_price: Decimal
    tax: ResolvedTax
    discount_percent: Decimal = Decimal("0")
    description: str = ""


@dataclass(frozen=True)
class InvoiceLine:
    name: str
    description: str
    quantity: Decimal
    unit_code: str
    unit_price: Decimal
    discount_amount: Decimal  # BG-27 line allowance
    net_amount: Decimal  # BT-131
    tax_category: str
    tax_rate: Decimal


@dataclass(frozen=True)
class TaxSubtotal:  # BG-23 plus the document-level allowance (BG-20) of that category
    category: str
    rate: Decimal
    exemption_reason: str
    allowance: Decimal
    basis: Decimal
    tax: Decimal


@dataclass(frozen=True)
class Invoice:
    number: str
    issue_date: dt.date
    seller: Party
    buyer: Party
    lines: list[InvoiceLine]
    tax_subtotals: list[TaxSubtotal]
    line_total: Decimal
    allowance_total: Decimal
    tax_basis_total: Decimal
    tax_total: Decimal
    grand_total: Decimal
    payment: PaymentInstructions
    buyer_reference: str
    service_date: dt.date | None = None
    service_period: tuple[dt.date, dt.date] | None = None
    due_date: dt.date | None = None
    payment_terms: str | None = None
    type_code: str = "380"  # 380 commercial invoice, 326 partial invoice, 381 credit note (Storno)
    prepaid_amount: Decimal = Decimal("0.00")  # BT-113: deducted Abschlagsrechnungen (gross)
    currency: str = "EUR"
    notes: list[str] = field(default_factory=list)
    preceding_invoice: PrecedingInvoice | None = None
    document_discount_percent: Decimal = Decimal("0")

    @property
    def due_amount(self) -> Decimal:  # BT-115 = BT-112 - BT-113 (BR-CO-16)
        return self.grand_total - self.prepaid_amount


def build_invoice(*, lines: list[LineData], document_discount_percent: Decimal = Decimal("0"), **fields) -> Invoice:
    totals = calculate(
        [LineInput(ln.quantity, ln.unit_price, ln.tax, ln.discount_percent) for ln in lines],
        document_discount_percent=document_discount_percent,
    )
    invoice_lines = [
        InvoiceLine(
            name=data.name,
            description=data.description,
            quantity=data.quantity,
            unit_code=data.unit_code,
            unit_price=data.unit_price,
            discount_amount=result.discount_amount,
            net_amount=result.net_amount,
            tax_category=result.tax.category,
            tax_rate=result.tax.rate,
        )
        for data, result in zip(lines, totals.lines, strict=True)
    ]
    subtotals = [
        TaxSubtotal(b.category, b.rate, b.exemption_reason, b.allowance, b.basis, b.tax) for b in totals.breakdown
    ]
    return Invoice(
        lines=invoice_lines,
        tax_subtotals=subtotals,
        line_total=totals.line_total,
        allowance_total=totals.allowance_total,
        tax_basis_total=totals.tax_basis_total,
        tax_total=totals.tax_total,
        grand_total=totals.grand_total,
        document_discount_percent=document_discount_percent,
        **fields,
    )
