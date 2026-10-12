"""Document workflow: drafts, finalizing (Festschreiben), quote → invoice, cancellation (Storno).

Taxes are resolved by the tax engine for the regime that applies on the service date.
Finalizing writes a snapshot (regime, per-line taxes, totals, party data) so later changes
of master data or tax status never alter a finalized document.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from skontio.catalog.models import Item, Unit
from skontio.documents.models import Document, DocumentLine, DocumentLocked, PartySnapshot
from skontio.documents.renditions import RenditionError, create_renditions
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import allocate_number
from skontio.tax.engine import LineInput, Regime, ResolvedTax, TaxError, TaxTreatment, Totals, calculate, resolve
from skontio.taxstatus.services import TaxStatusError, regime_for

__all__ = [
    "DocumentError",
    "DocumentLocked",
    "add_line",
    "add_line_from_item",
    "cancel_invoice",
    "confirm_ai_review",
    "convert_quote_to_invoice",
    "create_draft",
    "finalize",
    "preview_totals",
    "redact_party_snapshot",
    "replace_lines",
    "update_draft",
    "validation_problems",
]

Type = Document.Type

DEFAULT_PATTERNS = {
    Type.QUOTE: "AN-{YYYY}-{####}",
    Type.ORDER_CONFIRMATION: "AB-{YYYY}-{####}",
    Type.INVOICE: "RE-{YYYY}-{####}",
    Type.CANCELLATION: "ST-{YYYY}-{####}",
}
DEFAULT_QUOTE_VALIDITY_DAYS = 30
AI_ORIGINS = {Document.Origin.MCP, Document.Origin.ASSISTANT}


class DocumentError(ValueError):
    """A business rule prevents the requested document operation."""


# --- drafts -----------------------------------------------------------------------------------


def create_draft(
    company,
    doc_type: Type,
    customer,
    *,
    issue_date: dt.date,
    service_date: dt.date | None = None,
    service_period: tuple[dt.date, dt.date] | None = None,
    due_date: dt.date | None = None,
    valid_until: dt.date | None = None,
    origin: Document.Origin = Document.Origin.UI,
    created_by=None,
    idempotency_key: str = "",
    **texts,
) -> Document:
    if customer is not None and customer.company_id != company.pk:
        raise DocumentError("Der Kunde gehört nicht zu dieser Firma")
    if service_date and service_period:
        raise DocumentError("Entweder Leistungsdatum oder Leistungszeitraum angeben, nicht beides")
    period_start, period_end = service_period or (None, None)
    if period_start and period_end and period_end < period_start:
        raise DocumentError("Der Leistungszeitraum endet vor seinem Beginn")
    if doc_type == Type.INVOICE and due_date is None and customer is not None:
        due_date = issue_date + dt.timedelta(days=customer.payment_terms_days)
    if doc_type == Type.QUOTE and valid_until is None:
        valid_until = issue_date + dt.timedelta(days=DEFAULT_QUOTE_VALIDITY_DAYS)
    return Document.objects.create(
        company=company,
        doc_type=doc_type,
        customer=customer,
        issue_date=issue_date,
        service_date=service_date,
        service_period_start=period_start,
        service_period_end=period_end,
        due_date=due_date,
        valid_until=valid_until,
        origin=origin,
        ai_review_required=origin in AI_ORIGINS,
        created_by=created_by,
        idempotency_key=idempotency_key,
        **texts,
    )


def _require_draft(document: Document) -> None:
    if document.status != Document.Status.DRAFT:
        raise DocumentLocked(f"Dokument {document.number} ist festgeschrieben")


def add_line(
    document: Document,
    *,
    text: str,
    quantity: Decimal,
    unit_code: str,
    unit_price: Decimal,
    tax_treatment: TaxTreatment = TaxTreatment.STANDARD,
    discount_percent: Decimal = Decimal("0"),
    description: str = "",
    item: Item | None = None,
) -> DocumentLine:
    _require_draft(document)
    if not text.strip():
        raise DocumentError("Eine Position braucht eine Bezeichnung")
    if quantity < 0 or unit_price < 0:
        raise DocumentError("Menge und Einzelpreis dürfen nicht negativ sein")
    if not (Decimal("0") <= discount_percent <= Decimal("100")):
        raise DocumentError("Der Rabatt muss zwischen 0 und 100 % liegen")
    if not Unit.objects.filter(code=unit_code).exists():
        raise DocumentError(f"Unbekannte Einheit {unit_code!r}")
    position = (document.lines.aggregate(m=Max("position"))["m"] or 0) + 1
    return DocumentLine.objects.create(
        document=document,
        position=position,
        item=item,
        text=text.strip(),
        description=description.strip(),
        quantity=quantity,
        unit_code=unit_code,
        unit_price=unit_price,
        discount_percent=discount_percent,
        tax_treatment=TaxTreatment(tax_treatment),
    )


def add_line_from_item(
    document: Document, item: Item, *, quantity: Decimal, discount_percent: Decimal = Decimal("0")
) -> DocumentLine:
    """Copy the catalog entry into the document; later catalog changes do not affect it."""
    if item.company_id != document.company_id:
        raise DocumentError("Die Leistung gehört nicht zu dieser Firma")
    if item.archived_at is not None:
        raise DocumentError(f"Die Leistung {item.name!r} ist archiviert")
    return add_line(
        document,
        text=item.name,
        description=item.description,
        quantity=quantity,
        unit_code=item.unit.code,
        unit_price=item.net_price,
        tax_treatment=TaxTreatment(item.tax_treatment),
        discount_percent=discount_percent,
        item=item,
    )


EDITABLE_FIELDS = {
    "issue_date",
    "service_date",
    "service_period_start",
    "service_period_end",
    "due_date",
    "valid_until",
    "intro_text",
    "closing_text",
    "payment_terms_text",
    "document_discount_percent",
}


def update_draft(document: Document, *, origin: Document.Origin = Document.Origin.UI, **changes) -> Document:
    """Change header fields of a draft. A change by an AI client requires a new human review."""
    _require_draft(document)
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise DocumentError(f"Nicht änderbar: {', '.join(sorted(unknown))}")
    for field, value in changes.items():
        setattr(document, field, value)
    if document.service_date and (document.service_period_start or document.service_period_end):
        raise DocumentError("Entweder Leistungsdatum oder Leistungszeitraum angeben, nicht beides")
    if bool(document.service_period_start) != bool(document.service_period_end):
        raise DocumentError("Der Leistungszeitraum braucht Beginn und Ende")
    if document.service_period_start and document.service_period_end < document.service_period_start:
        raise DocumentError("Der Leistungszeitraum endet vor seinem Beginn")
    if not (Decimal("0") <= Decimal(document.document_discount_percent) <= Decimal("100")):
        raise DocumentError("Der Rabatt muss zwischen 0 und 100 % liegen")
    if origin in AI_ORIGINS:
        _mark_ai_touched(document)
    document.save()
    return document


def _mark_ai_touched(document: Document) -> None:
    document.ai_review_required = True
    document.ai_reviewed_by, document.ai_reviewed_at = None, None


def replace_lines(document: Document, lines: list[dict], *, origin: Document.Origin = Document.Origin.UI) -> None:
    """Replace all positions at once (``add_line`` / ``add_line_from_item`` arguments per entry)."""
    _require_draft(document)
    with transaction.atomic():
        document.lines.all().delete()
        for line in lines:
            line = dict(line)
            if (item := line.pop("item", None)) is not None:
                add_line_from_item(document, item, **line)
            else:
                add_line(document, **line)
        if origin in AI_ORIGINS:
            _mark_ai_touched(document)
            document.save(update_fields=["ai_review_required", "ai_reviewed_by", "ai_reviewed_at"])


def confirm_ai_review(document: Document, user) -> Document:
    """A person confirms they checked a draft created via MCP or the assistant."""
    _require_draft(document)
    document.ai_reviewed_by = user
    document.ai_reviewed_at = timezone.now()
    document.save(update_fields=["ai_reviewed_by", "ai_reviewed_at"])
    return document


# --- taxes ------------------------------------------------------------------------------------


def _tax_dates(document: Document) -> tuple[dt.date, dt.date]:
    if document.service_period_start and document.service_period_end:
        return document.service_period_start, document.service_period_end
    day = document.service_date or document.issue_date
    return day, day


def _regime(document: Document) -> Regime:
    start, end = _tax_dates(document)
    try:
        first, last = regime_for(document.company, start), regime_for(document.company, end)
    except TaxStatusError as exc:
        raise DocumentError(str(exc)) from exc
    if first is not last:
        raise DocumentError(
            "Der Leistungszeitraum umfasst einen Wechsel des Steuerstatus – bitte in zwei Rechnungen aufteilen"
        )
    return first


def _line_inputs(document: Document, regime: Regime) -> tuple[list[DocumentLine], list[LineInput]]:
    _, tax_date = _tax_dates(document)
    lines = list(document.lines.all())
    try:
        inputs = [
            LineInput(
                quantity=line.quantity,
                unit_price=line.unit_price,
                discount_percent=line.discount_percent,
                tax=resolve(TaxTreatment(line.tax_treatment), regime, tax_date),
            )
            for line in lines
        ]
    except TaxError as exc:
        raise DocumentError(str(exc)) from exc
    return lines, inputs


def _calculate(document: Document, inputs: list[LineInput]) -> Totals:
    try:
        return calculate(inputs, document_discount_percent=document.document_discount_percent)
    except TaxError as exc:
        raise DocumentError(str(exc)) from exc


def preview_totals(document: Document) -> Totals:
    """Totals as they would be finalized now – drafts follow tax status changes."""
    _, inputs = _line_inputs(document, _regime(document))
    return _calculate(document, inputs)


# --- finalizing -------------------------------------------------------------------------------


def _canonical_sha256(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _seller_data(company) -> dict:
    fields = (
        "name street postcode city country tax_number vat_id seller_identifier "
        "email phone contact_name iban bic account_holder"
    ).split()
    data = {f: getattr(company, f) for f in fields}
    if not data["vat_id"] and not data["seller_identifier"]:
        # BR-CO-26 needs BT-29/30/31; without USt-IdNr. the Steuernummer serves as seller identifier (ADR 0001)
        data["seller_identifier"] = data["tax_number"]
    return data


def _buyer_data(customer) -> dict:
    fields = "number kind name street postcode city country vat_id email buyer_reference".split()
    return {f: getattr(customer, f) for f in fields}


def validation_problems(document: Document) -> list[str]:
    """Everything that would block finalizing, as readable messages (also used by draft_validate)."""
    problems: list[str] = []
    company, customer = document.company, document.customer
    if document.ai_review_required and document.ai_reviewed_at is None:
        problems.append("Dieser KI-Entwurf braucht vor dem Festschreiben eine Prüfung durch einen Menschen")
    if customer is None:
        problems.append("Kunde fehlt")
    if document.doc_type in (Type.INVOICE, Type.CANCELLATION):
        # § 14 Abs. 4 UStG / § 34a UStDV
        if not (document.service_date or (document.service_period_start and document.service_period_end)):
            problems.append("Leistungsdatum oder Leistungszeitraum fehlt (§ 14 Abs. 4 Nr. 6 UStG)")
        if customer is not None and not (customer.street and customer.postcode and customer.city):
            problems.append("Vollständige Anschrift des Kunden fehlt")
        if not (company.name and company.street and company.postcode and company.city):
            problems.append("Vollständige Anschrift der eigenen Firma fehlt")
        if not (company.tax_number or company.vat_id):
            problems.append("Steuernummer oder USt-IdNr. der eigenen Firma fehlt")
        if document.doc_type == Type.INVOICE and not company.iban:
            problems.append("IBAN der eigenen Firma fehlt (Zahlungsangaben der E-Rechnung)")
    if not document.lines.exists():
        problems.append("Das Dokument hat keine Positionen")
    return problems


def _validate(document: Document) -> None:
    if problems := validation_problems(document):
        raise DocumentError("; ".join(problems))


def _store_snapshot(document: Document, role: PartySnapshot.Role, data: dict) -> str:
    digest = _canonical_sha256(data)
    PartySnapshot.objects.create(document=document, role=role, data=data, sha256=digest)
    return digest


def _seal(
    document: Document,
    regime: Regime,
    lines: list[DocumentLine],
    totals: Totals,
    seller: dict,
    buyer: dict,
) -> Document:
    """Write the snapshot and lock. Lines first – once the document is finalized they are locked."""
    if regime is Regime.SMALL_BUSINESS and totals.tax_total != 0:
        raise DocumentError("Kleinunternehmer dürfen keine Umsatzsteuer ausweisen (§ 14c UStG)")
    for line, result in zip(lines, totals.lines, strict=True):
        line.tax_category = result.tax.category
        line.tax_rate = result.tax.rate
        line.tax_exemption_reason = result.tax.exemption_reason
        line.net_amount = result.net_amount
        line.save(update_fields=["tax_category", "tax_rate", "tax_exemption_reason", "net_amount"])

    document.seller_snapshot_sha256 = _store_snapshot(document, PartySnapshot.Role.SELLER, seller)
    document.buyer_snapshot_sha256 = _store_snapshot(document, PartySnapshot.Role.BUYER, buyer)
    document.regime = regime
    document.tax_note = "\n".join(dict.fromkeys(b.exemption_reason for b in totals.breakdown if b.exemption_reason))
    document.net_total = totals.tax_basis_total
    document.tax_total = totals.tax_total
    document.gross_total = totals.grand_total

    NumberSequence.objects.get_or_create(
        company=document.company,
        doc_type=document.doc_type,
        defaults={"pattern": DEFAULT_PATTERNS[Type(document.doc_type)]},
    )
    document.number = allocate_number(document.company, document.doc_type, document.issue_date)
    document.status = Document.Status.FINALIZED
    document.locked_at = timezone.now()
    document.save()
    try:
        create_renditions(document)
    except (RenditionError, ValueError) as exc:
        raise DocumentError(str(exc)) from exc  # rolls back the whole finalize transaction
    return document


def finalize(document: Document) -> Document:
    """Festschreiben: validate, resolve taxes, snapshot, number and lock in one transaction."""
    with transaction.atomic():
        # of=("self",): Postgres cannot lock the nullable side of the customer outer join
        doc = Document.objects.select_for_update(of=("self",)).select_related("company", "customer").get(pk=document.pk)
        _require_draft(doc)
        if not doc.lines.exists():
            raise DocumentError("Ein Dokument ohne Positionen kann nicht festgeschrieben werden")
        _validate(doc)
        if doc.doc_type == Type.INVOICE and doc.due_date is None:
            doc.due_date = doc.issue_date + dt.timedelta(days=doc.customer.payment_terms_days)  # BR-CO-25
        regime = _regime(doc)
        lines, inputs = _line_inputs(doc, regime)
        totals = _calculate(doc, inputs)
        return _seal(doc, regime, lines, totals, _seller_data(doc.company), _buyer_data(doc.customer))


# --- follow-up documents ----------------------------------------------------------------------


def _copy_lines(source: Document, target: Document) -> list[DocumentLine]:
    return [
        DocumentLine.objects.create(
            document=target,
            position=line.position,
            item=line.item,
            text=line.text,
            description=line.description,
            quantity=line.quantity,
            unit_code=line.unit_code,
            unit_price=line.unit_price,
            discount_percent=line.discount_percent,
            tax_treatment=line.tax_treatment,
        )
        for line in source.lines.all()
    ]


def convert_quote_to_invoice(
    quote: Document,
    *,
    issue_date: dt.date,
    service_date: dt.date | None = None,
    service_period: tuple[dt.date, dt.date] | None = None,
    created_by=None,
    origin: Document.Origin = Document.Origin.UI,
    idempotency_key: str = "",
) -> Document:
    quote = Document.objects.select_related("company", "customer").get(pk=quote.pk)  # never trust stale objects
    if quote.doc_type != Type.QUOTE or quote.status != Document.Status.FINALIZED:
        raise DocumentError("Nur festgeschriebene Angebote können in eine Rechnung übernommen werden")
    with transaction.atomic():
        invoice = create_draft(
            quote.company,
            Type.INVOICE,
            quote.customer,
            issue_date=issue_date,
            service_date=service_date,
            service_period=service_period,
            created_by=created_by,
            origin=origin,
            idempotency_key=idempotency_key,
            intro_text=quote.intro_text,
            closing_text=quote.closing_text,
            document_discount_percent=quote.document_discount_percent,
            parent=quote,
        )
        _copy_lines(quote, invoice)
    return invoice


def cancel_invoice(invoice: Document, *, issue_date: dt.date, reason: str, created_by=None) -> Document:
    """Stornorechnung: mirrors the original's taxes and party data, finalized immediately."""
    invoice = Document.objects.select_related("company", "customer").get(pk=invoice.pk)
    if invoice.doc_type != Type.INVOICE or invoice.status != Document.Status.FINALIZED:
        raise DocumentError("Nur festgeschriebene Rechnungen können storniert werden")
    if not reason.strip():
        raise DocumentError("Für einen Storno ist ein Grund anzugeben")
    with transaction.atomic():
        if Document.objects.filter(parent=invoice, doc_type=Type.CANCELLATION).exists():
            raise DocumentError(f"Rechnung {invoice.number} ist bereits storniert")
        storno = Document.objects.create(
            company=invoice.company,
            doc_type=Type.CANCELLATION,
            customer=invoice.customer,
            parent=invoice,
            issue_date=issue_date,
            service_date=invoice.service_date,
            service_period_start=invoice.service_period_start,
            service_period_end=invoice.service_period_end,
            document_discount_percent=invoice.document_discount_percent,
            cancellation_reason=reason.strip(),
            created_by=created_by,
        )
        lines = _copy_lines(invoice, storno)
        original = {line.position: line for line in invoice.lines.all()}
        inputs = [
            LineInput(
                quantity=line.quantity,
                unit_price=line.unit_price,
                discount_percent=line.discount_percent,
                tax=ResolvedTax(
                    original[line.position].tax_category,
                    original[line.position].tax_rate,
                    original[line.position].tax_exemption_reason,
                ),
            )
            for line in lines
        ]
        snapshots = {s.role: s.data for s in invoice.party_snapshots.all()}
        return _seal(
            storno,
            Regime(invoice.regime),
            lines,
            _calculate(storno, inputs),
            snapshots[PartySnapshot.Role.SELLER],
            snapshots[PartySnapshot.Role.BUYER],
        )


# --- retention --------------------------------------------------------------------------------


def redact_party_snapshot(document: Document, role: PartySnapshot.Role) -> PartySnapshot:
    """Remove personal data after the retention period; the locked document keeps the hash."""
    snapshot = PartySnapshot.objects.get(document=document, role=role)
    snapshot.data = {"redacted": True}
    snapshot.redacted_at = timezone.now()
    snapshot.save(update_fields=["data", "redacted_at"])
    return snapshot
