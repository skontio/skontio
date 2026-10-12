"""Draft tools (plan §7.2). They create and change drafts only – never finalize, send, cancel or delete.

* The server computes taxes and totals; amounts sent by the model other than unit prices are not accepted.
* ``idempotency_key`` is mandatory: repeating a call returns the draft created the first time.
* Same customer and same positions within 10 minutes from an AI client is refused as a probable
  duplicate unless ``allow_duplicate`` is set.
* Every draft touched by an AI client is marked "KI-Entwurf, bitte prüfen"; finalizing requires a
  human review in the web interface.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal

from django.utils import timezone
from pydantic import model_validator

from skontio.catalog.models import Item
from skontio.documents.models import Document
from skontio.documents.services import (
    AI_ORIGINS,
    convert_quote_to_invoice,
    create_draft,
    replace_lines,
    update_draft,
)
from skontio.tax.engine import TaxTreatment
from skontio.tools.customers import find_customer
from skontio.tools.inputs import (
    CustomerNumber,
    DocumentNumber,
    IdempotencyKey,
    Input,
    Money,
    Name,
    Percent,
    Quantity,
    Text,
    UnitCode,
)
from skontio.tools.registry import Out, ToolError, tool
from skontio.tools.views import document_view, review_url

DUPLICATE_WINDOW = dt.timedelta(minutes=10)
MAX_LINES = 100


class LineInput(Input):
    """Either ``item_id`` (catalog values are used) or ``text`` + ``unit`` + ``unit_price``."""

    item_id: int | None = None
    text: Name | None = None
    description: Text = ""
    quantity: Quantity
    unit: UnitCode | None = None
    unit_price: Money | None = None
    tax_treatment: TaxTreatment | None = None
    discount_percent: Percent = Decimal("0")

    @model_validator(mode="after")
    def item_or_free_text(self):
        free = {"text": self.text, "unit": self.unit, "unit_price": self.unit_price}
        if self.item_id is not None:
            if any(v is not None for v in free.values()) or self.tax_treatment or self.description:
                raise ValueError("Mit item_id kommen Text, Einheit, Preis und Steuer aus dem Katalog – nicht mitsenden")
        elif any(v is None for v in free.values()):
            missing = ", ".join(k for k, v in free.items() if v is None)
            raise ValueError(f"Ohne item_id sind text, unit und unit_price Pflicht (fehlt: {missing})")
        return self


class DatesInput(Input):
    issue_date: dt.date | None = None
    service_date: dt.date | None = None
    service_period_start: dt.date | None = None
    service_period_end: dt.date | None = None

    @model_validator(mode="after")
    def one_service_date(self):
        if self.service_date and (self.service_period_start or self.service_period_end):
            raise ValueError("Entweder service_date oder service_period_start/-end angeben, nicht beides")
        if bool(self.service_period_start) != bool(self.service_period_end):
            raise ValueError("Der Leistungszeitraum braucht service_period_start und service_period_end")
        return self


class DraftInput(DatesInput):
    customer_number: CustomerNumber
    idempotency_key: IdempotencyKey
    lines: list[LineInput]
    intro_text: Text = ""
    closing_text: Text = ""
    document_discount_percent: Percent = Decimal("0")
    allow_duplicate: bool = False

    @model_validator(mode="after")
    def line_count(self):
        if not 1 <= len(self.lines) <= MAX_LINES:
            raise ValueError(f"1 bis {MAX_LINES} Positionen angeben")
        return self


class QuoteDraftInput(DraftInput):
    valid_until: dt.date | None = None


class InvoiceDraftInput(DraftInput):
    due_date: dt.date | None = None


def _origin(ctx) -> Document.Origin:
    return Document.Origin(ctx.actor_kind) if ctx.actor_kind in AI_ORIGINS else Document.Origin.API


def _line_args(ctx, lines: list[LineInput]) -> list[dict]:
    result = []
    for line in lines:
        if line.item_id is not None:
            item = Item.objects.select_related("unit").filter(company=ctx.company, pk=line.item_id).first()
            if item is None:
                raise ToolError(f"Leistung {line.item_id} nicht gefunden – catalog_search liefert gültige item_id")
            result.append({"item": item, "quantity": line.quantity, "discount_percent": line.discount_percent})
        else:
            result.append(
                {
                    "text": line.text,
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit_code": line.unit,
                    "unit_price": line.unit_price,
                    "tax_treatment": line.tax_treatment or TaxTreatment.STANDARD,
                    "discount_percent": line.discount_percent,
                }
            )
    return result


def _line_key(text, quantity, unit_code, unit_price, discount) -> tuple:
    return (
        text,
        format(Decimal(quantity).normalize(), "f"),
        unit_code,
        format(Decimal(unit_price).normalize(), "f"),
        format(Decimal(discount).normalize(), "f"),
    )


def _fingerprint(doc_type: str, customer_id: int, keys: list[tuple]) -> str:
    return hashlib.sha256(json.dumps([doc_type, customer_id, sorted(keys)]).encode()).hexdigest()


def _requested_keys(line_args: list[dict]) -> list[tuple]:
    keys = []
    for args in line_args:
        if item := args.get("item"):
            price = item.net_price
            keys.append(_line_key(item.name, args["quantity"], item.unit.code, price, args["discount_percent"]))
        else:
            keys.append(
                _line_key(
                    args["text"], args["quantity"], args["unit_code"], args["unit_price"], args["discount_percent"]
                )
            )
    return keys


def _recent_duplicate(ctx, doc_type, customer, fingerprint: str) -> Document | None:
    recent = Document.objects.filter(
        company=ctx.company,
        doc_type=doc_type,
        customer=customer,
        origin__in=AI_ORIGINS,
        created_at__gte=timezone.now() - DUPLICATE_WINDOW,
    ).prefetch_related("lines")
    for document in recent:
        keys = [
            _line_key(line.text, line.quantity, line.unit_code, line.unit_price, line.discount_percent)
            for line in document.lines.all()
        ]
        if _fingerprint(doc_type, customer.pk, keys) == fingerprint:
            return document
    return None


def _already_created(ctx, key: str, doc_type: str) -> Out | None:
    existing = Document.objects.filter(company=ctx.company, idempotency_key=key).first()
    if existing is None:
        return None
    if existing.doc_type != doc_type:
        raise ToolError("Dieser idempotency_key wurde schon für ein anderes Dokument verwendet")
    return Out({**document_view(existing, ctx), "already_created": True}, "document", existing.pk)


def _create(ctx, doc_type: Document.Type, args: DraftInput, **extra) -> Out:
    if done := _already_created(ctx, args.idempotency_key, doc_type):
        return done
    customer = find_customer(ctx, args.customer_number)
    line_args = _line_args(ctx, args.lines)
    if not args.allow_duplicate:
        fingerprint = _fingerprint(doc_type, customer.pk, _requested_keys(line_args))
        if duplicate := _recent_duplicate(ctx, doc_type, customer, fingerprint):
            raise ToolError(
                f"Möglicherweise doppelt: Entwurf {duplicate.pk} mit denselben Positionen wurde vor weniger als "
                "10 Minuten angelegt. Nur wenn wirklich ein zweites Dokument gewünscht ist, mit "
                "allow_duplicate=true wiederholen."
            )
    period = (args.service_period_start, args.service_period_end) if args.service_period_start else None
    document = create_draft(
        ctx.company,
        doc_type,
        customer,
        issue_date=args.issue_date or timezone.localdate(),
        service_date=args.service_date,
        service_period=period,
        origin=_origin(ctx),
        created_by=ctx.user,
        idempotency_key=args.idempotency_key,
        intro_text=args.intro_text,
        closing_text=args.closing_text,
        document_discount_percent=args.document_discount_percent,
        **{k: v for k, v in extra.items() if v is not None},
    )
    replace_lines(document, line_args, origin=document.origin)
    return Out(document_view(document, ctx), "document", document.pk)


@tool(
    name="quote_draft_create",
    title="Angebotsentwurf anlegen",
    scope="drafts:write",
    input_model=QuoteDraftInput,
    read_only=False,
    idempotent=True,
)
def quote_draft_create(ctx, args: QuoteDraftInput) -> Out:
    """Legt einen Angebotsentwurf an. Positionen entweder mit item_id aus catalog_search oder mit text,
    unit und unit_price (netto). Der Server berechnet Steuern und Summen selbst. Ergebnis ist ein Entwurf
    „KI-Entwurf, bitte prüfen“; festschreiben und versenden kann nur ein Mensch in Skontio (review_url)."""
    return _create(ctx, Document.Type.QUOTE, args, valid_until=args.valid_until)


@tool(
    name="invoice_draft_create",
    title="Rechnungsentwurf anlegen",
    scope="drafts:write",
    input_model=InvoiceDraftInput,
    read_only=False,
    idempotent=True,
)
def invoice_draft_create(ctx, args: InvoiceDraftInput) -> Out:
    """Legt einen Rechnungsentwurf an. Leistungsdatum (service_date) oder Leistungszeitraum ist Pflicht
    (§ 14 UStG). Positionen entweder mit item_id aus catalog_search oder mit text, unit und unit_price
    (netto). Der Server berechnet Steuern und Summen selbst. Ergebnis ist ein Entwurf „KI-Entwurf, bitte
    prüfen“; festschreiben und versenden kann nur ein Mensch in Skontio (review_url)."""
    return _create(ctx, Document.Type.INVOICE, args, due_date=args.due_date)


def _draft(ctx, document_id: int) -> Document:
    document = Document.objects.filter(company=ctx.company, pk=document_id).select_related("customer").first()
    if document is None:
        raise ToolError(f"Dokument {document_id} nicht gefunden")
    return document


class UpdateInput(DatesInput):
    document_id: int
    lines: list[LineInput] | None = None
    intro_text: Text | None = None
    closing_text: Text | None = None
    document_discount_percent: Percent | None = None
    due_date: dt.date | None = None
    valid_until: dt.date | None = None

    @model_validator(mode="after")
    def something_to_change(self):
        if not self.model_dump(exclude_none=True, exclude={"document_id"}):
            raise ValueError("Mindestens ein Feld zum Ändern angeben")
        if self.lines is not None and not 1 <= len(self.lines) <= MAX_LINES:
            raise ValueError(f"1 bis {MAX_LINES} Positionen angeben")
        return self


@tool(
    name="draft_update",
    title="Entwurf ändern",
    scope="drafts:write",
    input_model=UpdateInput,
    read_only=False,
    idempotent=True,
)
def draft_update(ctx, args: UpdateInput) -> Out:
    """Ändert einen Angebots- oder Rechnungsentwurf. ``lines`` ersetzt alle Positionen. Festgeschriebene
    Dokumente lassen sich nicht ändern. Nach jeder Änderung ist wieder eine menschliche Prüfung nötig."""
    document = _draft(ctx, args.document_id)
    if document.status != Document.Status.DRAFT:
        raise ToolError(f"Dokument {document.number} ist festgeschrieben – Korrektur nur per Storno in Skontio")
    changes = args.model_dump(exclude_none=True, exclude={"document_id", "lines"})
    if args.service_date:
        changes.update(service_period_start=None, service_period_end=None)
    elif args.service_period_start:
        changes["service_date"] = None
    if changes:
        update_draft(document, origin=_origin(ctx), **changes)
    if args.lines is not None:
        replace_lines(document, _line_args(ctx, args.lines), origin=_origin(ctx))
    document.refresh_from_db()
    return Out(document_view(document, ctx), "document", document.pk)


class ConvertInput(DatesInput):
    quote_number: DocumentNumber
    idempotency_key: IdempotencyKey


@tool(
    name="quote_to_invoice_draft",
    title="Angebot in Rechnungsentwurf übernehmen",
    scope="drafts:write",
    input_model=ConvertInput,
    read_only=False,
    idempotent=True,
)
def quote_to_invoice_draft(ctx, args: ConvertInput) -> Out:
    """Übernimmt ein festgeschriebenes Angebot (Nummer, z. B. AN-2026-0003) in einen Rechnungsentwurf.
    Das Leistungsdatum der Rechnung angeben (service_date oder Leistungszeitraum)."""
    if done := _already_created(ctx, args.idempotency_key, Document.Type.INVOICE):
        return done
    quote = Document.objects.filter(company=ctx.company, doc_type=Document.Type.QUOTE, number=args.quote_number).first()
    if quote is None:
        raise ToolError(f"Festgeschriebenes Angebot {args.quote_number!r} nicht gefunden")
    period = (args.service_period_start, args.service_period_end) if args.service_period_start else None
    invoice = convert_quote_to_invoice(
        quote,
        issue_date=args.issue_date or timezone.localdate(),
        service_date=args.service_date,
        service_period=period,
        created_by=ctx.user,
        origin=_origin(ctx),
        idempotency_key=args.idempotency_key,
    )
    return Out(document_view(invoice, ctx), "document", invoice.pk)


class DocumentInput(Input):
    document_id: int


@tool(name="draft_validate", title="Entwurf prüfen", scope="drafts:read", input_model=DocumentInput)
def draft_validate(ctx, args: DocumentInput) -> Out:
    """Prüft einen Entwurf: vom Server berechnete Summen und alles, was vor dem Festschreiben noch fehlt
    (problems_before_finalizing). Ändert nichts."""
    document = _draft(ctx, args.document_id)
    return Out(document_view(document, ctx), "document", document.pk)


@tool(name="document_preview_link", title="Link zum Prüfen", scope="drafts:read", input_model=DocumentInput)
def document_preview_link(ctx, args: DocumentInput) -> Out:
    """Liefert den Link, unter dem ein Mensch das Dokument in Skontio prüfen, festschreiben und
    versenden kann (Anmeldung erforderlich)."""
    document = _draft(ctx, args.document_id)
    return Out({"document_id": document.pk, "review_url": review_url(document)}, "document", document.pk)
