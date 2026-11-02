"""Zusammenfassende Meldung (§ 18a UStG) – a preparation for the BZStOnline-Portal, not a filing.

Reported per customer VAT ID: innergemeinschaftliche Lieferungen (Art L, tax category K) and
sonstige Leistungen to EU businesses under reverse charge (Art S, category AE with an EU buyer),
in the period the invoice was issued. Cancellations (stored like credit notes, positive) count
negative; a Schlussrechnung counts only what its deducted Abschlagsrechnungen did not already
report. Amounts in whole euros.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from skontio.documents.models import INVOICE_TYPES, Document, PartySnapshot
from skontio.documents.services import stored_totals
from skontio.reports.ustva import EU, Period

KINDS = {"L": "Innergemeinschaftliche Lieferungen", "S": "Sonstige Leistungen (§ 3a Abs. 2 UStG)"}


@dataclass(frozen=True)
class Row:
    country: str
    vat_id: str  # without the country prefix, as the portal asks for it
    kind: str  # "L" | "S"
    amount: Decimal

    @property
    def full_vat_id(self) -> str:
        return f"{self.country}{self.vat_id}"


@dataclass
class ZmReport:
    period: Period
    rows: list[Row]
    missing_vat_id: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def deadline(self) -> dt.date:
        """25th day after the end of the period (§ 18a Abs. 1 UStG); weekends move to Monday."""
        day = self.period.end + dt.timedelta(days=25)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        return day


def _kind(category: str, country: str) -> str | None:
    if category == "K":
        return "L"
    if category == "AE" and country in EU:
        return "S"
    return None


def zm_report(company, period: Period) -> ZmReport:
    sums: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    missing: list[str] = []
    documents = Document.objects.filter(
        company=company,
        status=Document.Status.FINALIZED,
        doc_type__in=[*INVOICE_TYPES, Document.Type.CANCELLATION],
        issue_date__range=(period.start, period.end),
    ).prefetch_related("lines", "deductions")
    for doc in documents:
        snapshot = PartySnapshot.objects.filter(document=doc, role=PartySnapshot.Role.BUYER).first()
        buyer = snapshot.data if snapshot else {}
        country = (buyer.get("country") or "").upper()
        vat_id = (buyer.get("vat_id") or "").replace(" ", "").upper()
        per_kind: dict[str, Decimal] = defaultdict(Decimal)
        for subtotal in stored_totals(doc).breakdown:
            if kind := _kind(subtotal.category, country):
                per_kind[kind] += subtotal.basis
        for deduction in doc.deductions.all():  # already reported with the Abschlagsrechnung
            if kind := _kind(deduction.tax_category, country):
                per_kind[kind] -= deduction.net_amount
        sign = -1 if doc.doc_type == Document.Type.CANCELLATION else 1  # Stornos are stored with positive amounts
        per_kind = {k: sign * v for k, v in per_kind.items() if v}
        if not per_kind:
            continue
        if not vat_id:
            missing.append(doc.number)
            continue
        prefix, rest = (vat_id[:2], vat_id[2:]) if vat_id[:2].isalpha() else (country, vat_id)
        prefix = "EL" if prefix == "GR" else prefix  # Greece reports as EL
        for kind, amount in per_kind.items():
            sums[(prefix, rest, kind)] += amount
    rows = [
        Row(country, vat_id, kind, amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        for (country, vat_id, kind), amount in sorted(sums.items())
        if amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    ]
    report = ZmReport(period, rows, missing)
    report.notes.append(
        "Vorbereitung für das BZStOnline-Portal, keine Steuerberatung. Gemeldet wird im Zeitraum der "
        "Rechnungsausstellung; Dreiecksgeschäfte und Lieferungen in Konsignationslager sind nicht enthalten."
    )
    if missing:
        report.notes.append(
            "Ohne USt-IdNr. des Kunden lässt sich nicht melden – und die Steuerbefreiung bzw. Reverse Charge "
            f"setzt sie voraus: {', '.join(missing)}."
        )
    return report
