from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from django.utils import timezone
from pydantic import Field

from skontio.documents.models import Document
from skontio.payments.services import open_amount, payment_status
from skontio.tools.inputs import DocumentNumber, Input, Limit
from skontio.tools.registry import Out, ToolError, tool
from skontio.tools.views import customer_view, document_view, iso, money


def _invoices(ctx):
    return Document.objects.filter(
        company=ctx.company, doc_type=Document.Type.INVOICE, status=Document.Status.FINALIZED
    ).select_related("customer")


def _summary(document: Document, ctx) -> dict:
    today = timezone.localdate()
    remaining = open_amount(document)
    return {
        "number": document.number,
        "customer": customer_view(document.customer, ctx),
        "issue_date": iso(document.issue_date),
        "due_date": iso(document.due_date),
        "gross": money(document.gross_total),
        "open_amount": money(remaining),
        "payment_status": payment_status(document),
        "cancelled": document.children.filter(doc_type=Document.Type.CANCELLATION).exists(),
        "overdue": bool(remaining > 0 and document.due_date and document.due_date < today),
    }


class ListInput(Input):
    status: Literal["all", "open", "paid"] = "all"
    year: Annotated[int, Field(ge=2000, le=2100)] | None = None
    limit: Limit = 25


@tool(name="invoices_list", title="Rechnungen auflisten", scope="documents:read", input_model=ListInput)
def invoices_list(ctx, args: ListInput) -> Out:
    """Listet festgeschriebene Rechnungen (neueste zuerst) mit Betrag, offenem Betrag und Zahlstatus."""
    invoices = _invoices(ctx)
    if args.year:
        invoices = invoices.filter(issue_date__year=args.year)
    result = []
    for invoice in invoices.order_by("-issue_date", "-pk"):
        summary = _summary(invoice, ctx)
        if args.status == "open" and summary["payment_status"] == "paid":
            continue
        if args.status == "paid" and summary["payment_status"] != "paid":
            continue
        result.append(summary)
        if len(result) >= args.limit:
            break
    return Out({"invoices": result})


class GetInput(Input):
    number: DocumentNumber


@tool(name="invoice_get", title="Rechnung abrufen", scope="documents:read", input_model=GetInput)
def invoice_get(ctx, args: GetInput) -> Out:
    """Liefert eine festgeschriebene Rechnung mit Positionen, Summen und Zahlstatus."""
    invoice = _invoices(ctx).filter(number=args.number).first()
    if invoice is None:
        raise ToolError(f"Rechnung {args.number!r} nicht gefunden")
    return Out({**document_view(invoice, ctx), **_summary(invoice, ctx)}, "document", invoice.pk)


class OpenItemsInput(Input):
    limit: Limit = 50


@tool(name="open_items", title="Offene Posten", scope="documents:read", input_model=OpenItemsInput)
def open_items(ctx, args: OpenItemsInput) -> Out:
    """Offene und teilbezahlte Rechnungen mit Summe und Kennzeichen für Überfälligkeit (stornierte ausgenommen)."""
    items, total = [], Decimal("0")
    for invoice in _invoices(ctx).order_by("due_date", "pk"):
        summary = _summary(invoice, ctx)
        if summary["payment_status"] == "paid" or summary["cancelled"]:
            continue
        total += open_amount(invoice)
        items.append(summary)
    return Out({"open_total": money(total), "count": len(items), "items": items[: args.limit]})
