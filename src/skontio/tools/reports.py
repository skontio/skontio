from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Annotated

from django.utils import timezone
from pydantic import Field

from skontio.documents.models import INVOICE_TYPES, Document
from skontio.taxstatus.thresholds import net_receipts, small_business_status
from skontio.tools.inputs import Input
from skontio.tools.registry import Out, tool
from skontio.tools.views import iso, money

Year = Annotated[int, Field(ge=2000, le=2100)]
NOTE = "Auswertung aus Skontio – keine Steuerberatung."


class YearInput(Input):
    year: Year | None = None


@tool(name="revenue_summary", title="Umsatz je Monat", scope="reports:read", input_model=YearInput)
def revenue_summary(ctx, args: YearInput) -> Out:
    """Netto-Umsatz eines Jahres je Monat: berechnet (Rechnungen abzüglich Stornos, nach Rechnungsdatum)
    und vereinnahmt (Zahlungseingänge, Netto-Anteil – maßgeblich bei Ist-Versteuerung)."""
    year = args.year or timezone.localdate().year
    invoiced: dict[int, Decimal] = defaultdict(Decimal)
    documents = Document.objects.filter(
        company=ctx.company,
        status=Document.Status.FINALIZED,
        doc_type__in=[*INVOICE_TYPES, Document.Type.CANCELLATION],
        issue_date__year=year,
    ).prefetch_related("deductions")
    for document in documents:
        # Storno amounts are stored positive (type code 381); deducted partial invoices were counted already
        sign = -1 if document.doc_type == Document.Type.CANCELLATION else 1
        net = (document.net_total or 0) - sum((d.net_amount for d in document.deductions.all()), Decimal("0"))
        invoiced[document.issue_date.month] += sign * net
    received: dict[int, Decimal] = defaultdict(Decimal)
    for day, amount in net_receipts(ctx.company, year):
        received[day.month] += amount
    months = [
        {"month": m, "invoiced_net": money(invoiced[m]), "received_net": money(received[m])} for m in range(1, 13)
    ]
    return Out(
        {
            "year": year,
            "months": months,
            "invoiced_net_total": money(sum(invoiced.values(), Decimal("0"))),
            "received_net_total": money(sum(received.values(), Decimal("0"))),
            "note": NOTE,
        }
    )


@tool(name="small_business_status", title="Kleinunternehmergrenze", scope="reports:read", input_model=Input)
def small_business_status_tool(ctx, args: Input) -> Out:
    """Stand der Kleinunternehmergrenze (§ 19 UStG) im laufenden Jahr nach vereinnahmten Netto-Umsätzen."""
    status = small_business_status(ctx.company, timezone.localdate())
    return Out(
        {
            "year": status.year,
            "revenue": money(status.revenue),
            "current_year_limit": money(status.current_limit),
            "percent_of_limit": str(status.percent),
            "level": status.level.value,
            "crossed_on": iso(status.crossed_on),
            "previous_year_revenue": money(status.previous_year_revenue),
            "previous_year_limit": money(status.previous_year_limit),
            "previous_year_exceeded": status.previous_year_exceeded,
            "source": status.rule.source,
            "note": NOTE,
        }
    )
