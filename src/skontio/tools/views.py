"""What tools may reveal (plan §7.3): field allowlists, pseudonyms, clipped and marked free text.

* Customers: only number, name, kind and country by default; private persons appear as
  "Privatperson K-00012" while pseudonym mode is on (default). Never e-mail, phone, street or IBAN.
* Free text written by people (descriptions, intro/closing texts) is returned as
  ``{"untrusted_text": …}`` so a client can tell data from instructions, and is clipped.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from django.conf import settings

from skontio.crm.models import Customer
from skontio.documents.models import Document
from skontio.documents.services import DocumentError, prepaid_total, preview_totals, validation_problems

CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‪-‮⁦-⁩]")  # incl. bidi overrides
NAME_LIMIT, TEXT_LIMIT = 200, 1000


def clip(value: str, limit: int = NAME_LIMIT) -> str:
    value = CONTROL.sub("", value or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def untrusted(value: str, limit: int = TEXT_LIMIT) -> dict | None:
    value = clip(value, limit)
    return {"untrusted_text": value} if value else None


def money(value: Decimal | None) -> str | None:
    return None if value is None else str(Decimal(value).quantize(Decimal("0.01")))


def number(value: Decimal) -> str:
    return format(Decimal(value).normalize(), "f")


def iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def customer_view(customer: Customer | None, ctx, *, detail: bool = False) -> dict | None:
    if customer is None:
        return None
    pseudonymised = ctx.pseudonymise_persons and customer.kind == Customer.Kind.PERSON
    view = {
        "number": customer.number,
        "name": f"Privatperson {customer.number}" if pseudonymised else clip(customer.name),
        "kind": customer.kind,
        "country": customer.country,
        "pseudonymised": pseudonymised,
    }
    if detail:
        view["payment_terms_days"] = customer.payment_terms_days
        view["address_complete"] = bool(customer.street and customer.postcode and customer.city)
        if customer.kind == Customer.Kind.ORGANISATION:
            view["vat_id"] = customer.vat_id
            view["buyer_reference"] = clip(customer.buyer_reference, 100)
    return view


def item_view(item) -> dict:
    return {
        "item_id": item.pk,
        "name": clip(item.name),
        "sku": item.sku,
        "kind": item.kind,
        "unit": item.unit.code,
        "unit_label": item.unit.label_de,
        "net_price": number(item.net_price),
        "tax_treatment": item.tax_treatment,
        "description": untrusted(item.description),
        "archived": item.archived_at is not None,
    }


def review_url(document: Document) -> str:
    return f"{settings.PUBLIC_URL}/dokumente/{document.pk}/"


def document_view(document: Document, ctx, *, with_lines: bool = True) -> dict:
    view = {
        "document_id": document.pk,
        "type": document.doc_type,
        "status": document.status,
        "number": document.number,
        "language": document.language,
        "customer": customer_view(document.customer, ctx),
        "issue_date": iso(document.issue_date),
        "service_date": iso(document.service_date),
        "service_period": (
            {"start": iso(document.service_period_start), "end": iso(document.service_period_end)}
            if document.service_period_start
            else None
        ),
        "due_date": iso(document.due_date),
        "valid_until": iso(document.valid_until),
        "origin": document.origin,
        "ai_review_required": document.ai_review_required and document.ai_reviewed_at is None,
        "review_url": review_url(document),
    }
    if document.status == Document.Status.FINALIZED:
        prepaid = prepaid_total(document)
        view["totals"] = {
            "net": money(document.net_total),
            "tax": money(document.tax_total),
            "gross": money(document.gross_total),
        }
        if prepaid:
            view["totals"] |= {"prepaid": money(prepaid), "due": money(document.gross_total - prepaid)}
    else:
        try:
            totals = preview_totals(document)
            view["totals"] = {
                "net": money(totals.tax_basis_total),
                "tax": money(totals.tax_total),
                "gross": money(totals.grand_total),
                "calculated_by": "server",
            }
            if document.doc_type == Document.Type.FINAL_INVOICE:
                prepaid = prepaid_total(document)
                view["totals"] |= {"prepaid": money(prepaid), "due": money(totals.grand_total - prepaid)}
        except DocumentError as exc:
            view["totals"] = None
            view["totals_error"] = str(exc)
        view["problems_before_finalizing"] = validation_problems(document)
    if with_lines:
        view["intro_text"] = untrusted(document.intro_text)
        view["closing_text"] = untrusted(document.closing_text)
        view["document_discount_percent"] = number(document.document_discount_percent)
        view["lines"] = [
            {
                "position": line.position,
                "text": clip(line.text),
                "description": untrusted(line.description),
                "item_id": line.item_id,
                "quantity": number(line.quantity),
                "unit": line.unit_code,
                "unit_price": number(line.unit_price),
                "discount_percent": number(line.discount_percent),
                "tax_treatment": line.tax_treatment,
            }
            for line in document.lines.all()
        ]
    return view
