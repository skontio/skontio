"""DATEV-Export im Format EXTF "Buchungsstapel" (Version 700, Kategorie 21, Formatversion 13).

Istversteuerung/EÜR: one booking per payment received (split by tax rate of the invoice) and per paid
expense, always against the money account (bank, cash, transit, private). Amounts are positive; the
direction is given by "S"/"H" relative to the field "Konto". One file covers at most one calendar year
(DATEV requires all bookings of a batch to lie in one fiscal year).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from skontio.accounting.accounts import AccountError, chart_of_accounts, ledger_settings, validate_account
from skontio.accounting.datev_columns import COLUMNS
from skontio.documents.services import stored_totals
from skontio.expenses import income as other_income
from skontio.expenses.models import Expense
from skontio.payments.models import Payment
from skontio.tax.engine import Regime
from skontio.taxstatus.services import TaxStatusError, regime_for

CENT = Decimal("0.01")
INCOME_BY_CATEGORY = {
    "Z": "income_tax_free",
    "E": "income_tax_free",
    "AE": "income_reverse_charge",
    "K": "income_intra_eu",
    "G": "income_export",
    "O": "income_not_taxable",
}
INPUT_VAT_KEY = {Decimal("19"): "9", Decimal("7"): "8"}  # DATEV BU-Schlüssel Vorsteuer
MONEY_BY_PAYMENT = {Payment.Method.CASH: "cash", Payment.Method.PAYPAL: "transit"}
MONEY_BY_EXPENSE = {Expense.Method.CASH: "cash", Expense.Method.PRIVATE: "private"}
_DOCUMENT_FIELD = re.compile(r"[^A-Za-z0-9$&%*+\-/]")
_FORMULA_START = re.compile(r"^[=+@\t\r-]+")  # never start a text cell like a spreadsheet formula


class DatevError(ValueError):
    pass


@dataclass(frozen=True)
class Booking:
    amount: Decimal  # always > 0
    side: str  # "S" | "H" for the field Konto
    account: str
    counter_account: str
    bu_key: str
    date: dt.date
    document_field: str
    text: str
    review: bool = False  # needs a look by the tax advisor (e.g. § 13b)


def _money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _document_field(value: str) -> str:
    return _DOCUMENT_FIELD.sub("", value or "")[:36]


def _text(value: str) -> str:
    value = " ".join((value or "").split())
    return _FORMULA_START.sub("", value)[:60]


def _income_key(document, category: str, rate: Decimal) -> str:
    if document.regime == Regime.SMALL_BUSINESS:
        return "income_small_business"
    if category == "S":
        return "income_standard" if rate > 10 else "income_reduced"
    return INCOME_BY_CATEGORY.get(category, "income_tax_free")


def _split(amount: Decimal, shares: list[tuple[str, Decimal]]) -> list[tuple[str, Decimal]]:
    """Split a payment by the gross share of each tax group; the last part takes the rounding rest."""
    total = sum((share for _, share in shares), Decimal("0"))
    if not shares or not total:
        return [(shares[0][0] if shares else "income_tax_free", amount)]
    parts, rest = [], amount
    for index, (key, share) in enumerate(shares):
        part = rest if index == len(shares) - 1 else _money(amount * share / total)
        parts.append((key, part))
        rest -= part
    merged: dict[str, Decimal] = {}
    for key, part in parts:
        merged[key] = merged.get(key, Decimal("0")) + part
    return [(key, part) for key, part in merged.items() if part]


def _payment_bookings(company, start: dt.date, end: dt.date, accounts: dict[str, str]) -> list[Booking]:
    bookings = []
    payments = (
        Payment.objects.filter(company=company, received_on__range=(start, end))
        .select_related("document", "document__customer")
        .order_by("received_on", "pk")
    )
    for payment in payments:
        doc = payment.document
        totals = stored_totals(doc)
        shares = [(_income_key(doc, b.category, b.rate), b.basis + b.tax) for b in totals.breakdown]
        money = accounts[MONEY_BY_PAYMENT.get(payment.method, "bank")]
        customer = doc.customer.name if doc.customer else ""
        for key, part in _split(_money(payment.amount), shares):
            bookings.append(
                Booking(
                    amount=abs(part),
                    side="S" if part > 0 else "H",
                    account=money,
                    counter_account=accounts[key],
                    bu_key="",
                    date=payment.received_on,
                    document_field=_document_field(doc.number or ""),
                    text=_text(f"{customer} {doc.number or ''}"),
                )  # fmt: skip
            )
    return bookings


INCOME_BY_RATE = {Decimal("19"): "income_standard", Decimal("7"): "income_reduced"}


def _other_income_bookings(company, start: dt.date, end: dt.date, accounts: dict[str, str]) -> list[Booking]:
    bookings = []
    for income in other_income.active(company, start, end):
        if other_income.regime_on(company, income.received_on) is Regime.SMALL_BUSINESS:
            key = "income_small_business"
        else:
            key = INCOME_BY_RATE.get(Decimal(income.vat_rate), "income_tax_free")
        bookings.append(
            Booking(
                amount=_money(income.gross_amount),
                side="S",
                account=accounts[MONEY_BY_EXPENSE.get(income.method, "bank")],
                counter_account=accounts[key],
                bu_key="",
                date=income.received_on,
                document_field=_document_field(income.number),
                text=_text(f"{income.payer} {income.description}".strip()),
            )  # fmt: skip
        )
    return bookings


def _expense_bookings(company, start: dt.date, end: dt.date, accounts: dict[str, str]) -> list[Booking]:
    bookings, regimes = [], {}
    expenses = Expense.objects.filter(company=company, paid_on__range=(start, end), cancelled_at__isnull=True).order_by(
        "paid_on", "pk"
    )
    for expense in expenses:
        if expense.receipt_date not in regimes:
            try:
                regimes[expense.receipt_date] = regime_for(company, expense.receipt_date)
            except TaxStatusError:
                regimes[expense.receipt_date] = Regime.SMALL_BUSINESS
        standard = regimes[expense.receipt_date] is Regime.STANDARD
        bu_key = INPUT_VAT_KEY.get(Decimal(expense.vat_rate), "") if standard and not expense.reverse_charge else ""
        text = f"{expense.supplier} {expense.description}".strip()
        if expense.reverse_charge:
            text = "§13b prüfen: " + text
        bookings.append(
            Booking(
                amount=_money(expense.gross_amount),
                side="S",
                account=accounts[f"expense_{expense.category}"],
                counter_account=accounts[MONEY_BY_EXPENSE.get(expense.method, "bank")],
                bu_key=bu_key,
                date=expense.paid_on,
                document_field=_document_field(expense.number),
                text=_text(text),
                review=expense.reverse_charge,
            )  # fmt: skip
        )
    return bookings


def bookings(company, start: dt.date, end: dt.date) -> list[Booking]:
    if start > end:
        raise DatevError("Der Zeitraum endet vor seinem Beginn.")
    if start.year != end.year:
        raise DatevError("Ein DATEV-Stapel darf nur ein Wirtschaftsjahr enthalten – bitte je Kalenderjahr exportieren.")
    settings = ledger_settings(company)
    accounts = chart_of_accounts(company)
    try:
        for account in accounts.values():
            validate_account(account, settings.account_length)
    except AccountError as exc:
        raise DatevError(f"{exc} Bitte unter Einstellungen → Buchhaltung korrigieren.") from exc
    rows = (
        _payment_bookings(company, start, end, accounts)
        + _other_income_bookings(company, start, end, accounts)
        + _expense_bookings(company, start, end, accounts)
    )
    return sorted(rows, key=lambda b: (b.date, b.document_field))


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"' if value else ""


def _amount(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


def header(company, start: dt.date, end: dt.date, *, created: dt.datetime | None = None) -> list[str]:
    settings = ledger_settings(company)
    if not settings.consultant_number or not settings.client_number:
        raise DatevError("Berater- und Mandantennummer fehlen – bitte unter Einstellungen → Buchhaltung eintragen.")
    created = created or timezone.now()
    chart = "03" if settings.chart == "skr03" else "04"
    return [
        _quote("EXTF"), "700", "21", _quote("Buchungsstapel"), "13", created.strftime("%Y%m%d%H%M%S%f")[:17], "",
        _quote("RE"), _quote("Skontio"), "", str(settings.consultant_number), str(settings.client_number),
        f"{start.year}0101", str(settings.account_length), start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
        _quote(f"Skontio {start:%d.%m.}–{end:%d.%m.%Y}"), "", "1", "0", "0", _quote("EUR"), "", "", "", "",
        _quote(chart), "", "", "", "",
    ]  # fmt: skip


def export(company, start: dt.date, end: dt.date, *, created: dt.datetime | None = None) -> bytes:
    """The EXTF file, encoded as DATEV expects (Windows-1252, CRLF, semicolons)."""
    head = header(company, start, end, created=created)
    rows = bookings(company, start, end)
    lines = [";".join(head), ";".join(COLUMNS)]
    width = len(COLUMNS)
    for b in rows:
        cells = [""] * width
        cells[0], cells[1], cells[2] = _amount(b.amount), _quote(b.side), _quote("EUR")
        cells[6], cells[7], cells[8] = b.account, b.counter_account, _quote(b.bu_key)
        cells[9], cells[10], cells[13] = b.date.strftime("%d%m"), _quote(b.document_field), _quote(b.text)
        lines.append(";".join(cells))
    return ("\r\n".join(lines) + "\r\n").encode("cp1252", errors="replace")


def filename(start: dt.date, end: dt.date) -> str:
    return f"EXTF_Buchungsstapel_{start:%Y%m%d}_{end:%Y%m%d}.csv"
