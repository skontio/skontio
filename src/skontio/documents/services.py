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
from django.db.models import Max, Sum
from django.utils import timezone

from skontio.catalog.models import Item, Unit
from skontio.documents.languages import LANGUAGES, Lang
from skontio.documents.languages import normalise as normalise_language
from skontio.documents.layouts import template_for, text_block
from skontio.documents.models import (
    BASIS_TYPES,
    INVOICE_TYPES,
    Document,
    DocumentLine,
    DocumentLocked,
    PartySnapshot,
    PrepaymentDeduction,
)
from skontio.documents.renditions import RenditionError, create_renditions, render_preview_pdf
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import allocate_number
from skontio.tax.engine import (
    CENT,
    LineInput,
    Regime,
    ResolvedTax,
    TaxError,
    TaxTreatment,
    Totals,
    calculate,
    resolve,
)
from skontio.taxstatus.services import TaxStatusError, regime_for

__all__ = [
    "DocumentError",
    "DocumentLocked",
    "add_line",
    "add_deduction",
    "add_line_from_item",
    "amount_due",
    "cancel_invoice",
    "confirm_ai_review",
    "convert_quote_to_invoice",
    "create_draft",
    "create_final_invoice",
    "create_order_confirmation",
    "create_partial_invoice",
    "eligible_partial_invoices",
    "finalize",
    "open_partials_for",
    "prepaid_total",
    "preview_totals",
    "redact_party_snapshot",
    "remove_deduction",
    "stored_totals",
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
# Abschlags- und Schlussrechnungen are invoices: one gap-free invoice number range for all of them
SEQUENCE_TYPE = {Type.PARTIAL_INVOICE: Type.INVOICE, Type.FINAL_INVOICE: Type.INVOICE}
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
    language: str | None = None,
    **texts,
) -> Document:
    if customer is not None and customer.company_id != company.pk:
        raise DocumentError("Der Kunde gehört nicht zu dieser Firma")
    if service_date and service_period:
        raise DocumentError("Entweder Leistungsdatum oder Leistungszeitraum angeben, nicht beides")
    period_start, period_end = service_period or (None, None)
    if period_start and period_end and period_end < period_start:
        raise DocumentError("Der Leistungszeitraum endet vor seinem Beginn")
    if doc_type in INVOICE_TYPES and due_date is None and customer is not None:
        due_date = issue_date + dt.timedelta(days=customer.payment_terms_days)
    if doc_type == Type.QUOTE and valid_until is None:
        valid_until = issue_date + dt.timedelta(days=DEFAULT_QUOTE_VALIDITY_DAYS)
    if language is None:
        language = normalise_language(customer.language if customer is not None else None)
    elif language not in LANGUAGES:
        raise DocumentError(f"Unbekannte Dokumentsprache: {language}")
    if block := text_block(company, doc_type, language):
        for field in ("intro_text", "closing_text"):
            if not texts.get(field):
                texts[field] = getattr(block, field)
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
        language=language,
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
    "language",
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
    if document.language not in LANGUAGES:
        raise DocumentError(f"Unbekannte Dokumentsprache: {document.language}")
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


def supply_regime(document: Document) -> Regime:
    """The confirmed tax status for the document's date of supply as it stands today."""
    return _regime(document)


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


def preview_pdf(document: Document) -> bytes:
    """The draft as PDF with current master data, marked as draft (no number, no GiroCode)."""
    _require_draft(document)
    if document.customer is None or not document.lines.exists():
        raise DocumentError("Für die Vorschau braucht der Entwurf einen Kunden und mindestens eine Position")
    _, inputs = _line_inputs(document, _regime(document))
    _calculate(document, inputs)  # readable error instead of a broken PDF
    return render_preview_pdf(document, inputs, _seller_data(document.company), _buyer_data(document.customer))


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
    if document.doc_type in INVOICE_TYPES | {Type.CANCELLATION}:
        # § 14 Abs. 4 UStG / § 34a UStDV
        if not (document.service_date or (document.service_period_start and document.service_period_end)):
            problems.append("Leistungsdatum oder Leistungszeitraum fehlt (§ 14 Abs. 4 Nr. 6 UStG)")
        if customer is not None and not (customer.street and customer.postcode and customer.city):
            problems.append("Vollständige Anschrift des Kunden fehlt")
        if not (company.name and company.street and company.postcode and company.city):
            problems.append("Vollständige Anschrift der eigenen Firma fehlt")
        if not (company.tax_number or company.vat_id):
            problems.append("Steuernummer oder USt-IdNr. der eigenen Firma fehlt")
        if document.doc_type in INVOICE_TYPES and not company.iban:
            problems.append("IBAN der eigenen Firma fehlt (Zahlungsangaben der E-Rechnung)")
    if not document.lines.exists():
        problems.append("Das Dokument hat keine Positionen")
    if document.doc_type == Type.FINAL_INVOICE:
        problems += _deduction_problems(document)
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
    if prepaid_total(document) > totals.grand_total:
        raise DocumentError("Die abgezogenen Abschlagsrechnungen übersteigen den Rechnungsbetrag")
    lang = Lang(document.language)
    for line, result in zip(lines, totals.lines, strict=True):
        line.tax_category = result.tax.category
        line.tax_rate = result.tax.rate
        line.tax_exemption_reason = lang.tax_note(result.tax.exemption_reason)
        line.net_amount = result.net_amount
        line.save(update_fields=["tax_category", "tax_rate", "tax_exemption_reason", "net_amount"])

    document.seller_snapshot_sha256 = _store_snapshot(document, PartySnapshot.Role.SELLER, seller)
    document.buyer_snapshot_sha256 = _store_snapshot(document, PartySnapshot.Role.BUYER, buyer)
    document.regime = regime
    document.template = template_for(document)  # recorded: the archived PDF was rendered with it
    reasons = (lang.tax_note(b.exemption_reason) for b in totals.breakdown if b.exemption_reason)
    document.tax_note = "\n".join(dict.fromkeys(reasons))
    document.net_total = totals.tax_basis_total
    document.tax_total = totals.tax_total
    document.gross_total = totals.grand_total

    sequence = SEQUENCE_TYPE.get(Type(document.doc_type), Type(document.doc_type))
    NumberSequence.objects.get_or_create(
        company=document.company, doc_type=sequence, defaults={"pattern": DEFAULT_PATTERNS[sequence]}
    )
    document.number = allocate_number(document.company, sequence, document.issue_date)
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
        if doc.doc_type == Type.FINAL_INVOICE:
            # lock the deducted partial invoices: two final invoices must not deduct the same one
            sources = doc.deductions.values_list("source_id", flat=True)
            list(Document.objects.select_for_update().filter(pk__in=sources))
            if problems := _deduction_problems(doc):
                raise DocumentError("; ".join(problems))
        if doc.doc_type in INVOICE_TYPES and doc.due_date is None:
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
    quote = _require_basis(quote)  # never trust stale objects
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
            language=quote.language,
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
    if invoice.doc_type not in INVOICE_TYPES or invoice.status != Document.Status.FINALIZED:
        raise DocumentError("Nur festgeschriebene Rechnungen können storniert werden")
    if final := _active_final_invoice(invoice):
        raise DocumentError(
            f"Abschlagsrechnung {invoice.number} ist in der Schlussrechnung {final.number} abgezogen – "
            "zuerst die Schlussrechnung stornieren"
        )
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
            language=invoice.language,
            template=invoice.template,
            created_by=created_by,
        )
        lines = _copy_lines(invoice, storno)
        for deduction in invoice.deductions.all():  # a cancelled Schlussrechnung releases its partial invoices
            PrepaymentDeduction.objects.create(
                document=storno,
                source=deduction.source,
                tax_category=deduction.tax_category,
                tax_rate=deduction.tax_rate,
                net_amount=deduction.net_amount,
                tax_amount=deduction.tax_amount,
            )
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


# --- Abschlags- und Schlussrechnungen (§ 14 Abs. 5 UStG) -----------------------------------------


def stored_totals(document: Document) -> Totals:
    """Totals of a finalized document recomputed from its per-line tax snapshot (never current rates)."""
    inputs = [
        LineInput(
            quantity=line.quantity,
            unit_price=line.unit_price,
            discount_percent=line.discount_percent,
            tax=ResolvedTax(line.tax_category, line.tax_rate, line.tax_exemption_reason),
        )
        for line in document.lines.all()
    ]
    return calculate(inputs, document_discount_percent=document.document_discount_percent)


def prepaid_total(document: Document) -> Decimal:
    """Gross amount of all deducted Abschlagsrechnungen (EN 16931 BT-113)."""
    if document.pk is None:
        return Decimal("0.00")
    totals = document.deductions.aggregate(net=Sum("net_amount"), tax=Sum("tax_amount"))
    return (totals["net"] or Decimal("0.00")) + (totals["tax"] or Decimal("0.00"))


def amount_due(document: Document) -> Decimal:
    """What the customer still has to pay according to the document (BT-115)."""
    return (document.gross_total or Decimal("0.00")) - prepaid_total(document)


def _is_cancelled(document: Document) -> bool:
    return document.children.filter(doc_type=Type.CANCELLATION).exists()


def _active_final_invoice(partial: Document) -> Document | None:
    """The finalized, not cancelled Schlussrechnung that deducts this partial invoice, if any."""
    for deduction in partial.deducted_in.filter(
        document__doc_type=Type.FINAL_INVOICE, document__status=Document.Status.FINALIZED
    ).select_related("document"):
        if not _is_cancelled(deduction.document):
            return deduction.document
    return None


def _deduction_problems(document: Document) -> list[str]:
    problems = []
    sources = {d.source for d in document.deductions.select_related("source")}
    if not sources:
        problems.append("Eine Schlussrechnung braucht mindestens eine abgezogene Abschlagsrechnung")
    for source in sources:
        if source.customer_id != document.customer_id:
            problems.append(f"Abschlagsrechnung {source.number} gehört zu einem anderen Kunden")
        if _is_cancelled(source):
            problems.append(f"Abschlagsrechnung {source.number} ist storniert")
        final = _active_final_invoice(source)
        if final is not None and final.pk != document.pk:
            problems.append(f"Abschlagsrechnung {source.number} ist bereits in {final.number} abgezogen")
    return problems


def eligible_partial_invoices(company, customer, *, exclude: Document | None = None) -> list[Document]:
    """Finalized partial invoices of the customer that can still be deducted (optionally not yet in ``exclude``)."""
    candidates = Document.objects.filter(
        company=company, customer=customer, doc_type=Type.PARTIAL_INVOICE, status=Document.Status.FINALIZED
    )
    if exclude is not None:
        candidates = candidates.exclude(deducted_in__document=exclude)
    return [c for c in candidates.order_by("issue_date", "pk") if not _is_cancelled(c) and not _active_final_invoice(c)]


def add_deduction(document: Document, partial: Document) -> list[PrepaymentDeduction]:
    """Deduct a finalized Abschlagsrechnung in a Schlussrechnung draft, per VAT category of the partial."""
    _require_draft(document)
    if document.doc_type != Type.FINAL_INVOICE:
        raise DocumentError("Abschlagsrechnungen werden nur in einer Schlussrechnung abgezogen")
    partial = Document.objects.get(pk=partial.pk)
    if partial.doc_type != Type.PARTIAL_INVOICE or partial.status != Document.Status.FINALIZED:
        raise DocumentError("Nur festgeschriebene Abschlagsrechnungen können abgezogen werden")
    if partial.company_id != document.company_id or partial.customer_id != document.customer_id:
        raise DocumentError(f"Abschlagsrechnung {partial.number} gehört zu einem anderen Kunden")
    if _is_cancelled(partial):
        raise DocumentError(f"Abschlagsrechnung {partial.number} ist storniert")
    if final := _active_final_invoice(partial):
        raise DocumentError(f"Abschlagsrechnung {partial.number} ist bereits in {final.number} abgezogen")
    if document.deductions.filter(source=partial).exists():
        raise DocumentError(f"Abschlagsrechnung {partial.number} ist bereits abgezogen")
    return [
        PrepaymentDeduction.objects.create(
            document=document,
            source=partial,
            tax_category=b.category,
            tax_rate=b.rate,
            net_amount=b.basis,
            tax_amount=b.tax,
        )
        for b in stored_totals(partial).breakdown
    ]


def remove_deduction(document: Document, partial: Document) -> None:
    _require_draft(document)
    document.deductions.filter(source=partial).delete()


def _partial_lines(quote: Document, *, percent: Decimal | None, net_amount: Decimal | None) -> list[dict]:
    """Split the requested share over the quote's tax treatments (proportional to their net amounts)."""
    lang = Lang(quote.language)
    title = lang.title(quote.doc_type)
    if (percent is None) == (net_amount is None):
        raise DocumentError("Entweder einen Prozentsatz oder einen Nettobetrag angeben")
    groups: dict[str, Decimal] = {}
    for line, result in zip(quote.lines.all(), stored_totals(quote).lines, strict=True):
        groups[line.tax_treatment] = groups.get(line.tax_treatment, Decimal("0")) + result.net_amount
    factor = Decimal("1") - Decimal(quote.document_discount_percent) / 100
    groups = {treatment: net * factor for treatment, net in groups.items()}
    total = sum(groups.values(), Decimal("0"))
    if total <= 0:
        raise DocumentError(f"{quote.get_doc_type_display()} {quote.number} hat keinen positiven Nettobetrag")
    if percent is not None:
        if not Decimal("0") < percent <= Decimal("100"):
            raise DocumentError("Der Abschlag muss zwischen 0 und 100 % liegen")
        share = percent / 100
        label = lang("down_payment_percent", percent=lang.quantity(percent), title=title, number=quote.number)
    else:
        if not Decimal("0") < net_amount <= total:
            raise DocumentError(f"Der Abschlag muss zwischen 0 und {total.quantize(CENT)} € (netto) liegen")
        share = net_amount / total
        label = lang("down_payment", title=title, number=quote.number)
    lines, remaining = [], (total * share).quantize(CENT)
    treatments = sorted(groups, key=lambda t: groups[t])
    for index, treatment in enumerate(treatments):
        # rounding difference goes into the largest group so the parts add up to the requested amount
        amount = remaining if index == len(treatments) - 1 else (groups[treatment] * share).quantize(CENT)
        remaining -= amount
        suffix = f" ({lang.treatment(treatment)})" if len(groups) > 1 else ""
        lines.append(
            {
                "text": label + suffix,
                "quantity": Decimal("1"),
                "unit_code": "LS",
                "unit_price": amount,
                "tax_treatment": TaxTreatment(treatment),
            }
        )
    return lines


def _require_basis(basis: Document) -> Document:
    """A finalized quote or order confirmation, freshly loaded."""
    basis = Document.objects.select_related("company", "customer").get(pk=basis.pk)
    if basis.doc_type not in BASIS_TYPES or basis.status != Document.Status.FINALIZED:
        raise DocumentError("Nur aus festgeschriebenen Angeboten oder Auftragsbestätigungen")
    return basis


def _related_basis_ids(basis: Document) -> set[int]:
    """Quote and its order confirmations belong to the same order (Abschläge may hang on either)."""
    quote_id = basis.parent_id if basis.doc_type == Type.ORDER_CONFIRMATION and basis.parent_id else basis.pk
    confirmations = Document.objects.filter(parent_id=quote_id, doc_type=Type.ORDER_CONFIRMATION)
    return {quote_id, basis.pk, *confirmations.values_list("pk", flat=True)}


def open_partials_for(basis: Document) -> list[Document]:
    ids = _related_basis_ids(basis)
    return [p for p in eligible_partial_invoices(basis.company, basis.customer) if p.parent_id in ids]


def create_order_confirmation(
    quote: Document,
    *,
    issue_date: dt.date,
    created_by=None,
    origin: Document.Origin = Document.Origin.UI,
    idempotency_key: str = "",
) -> Document:
    """Auftragsbestätigung as a draft copy of a finalized quote."""
    quote = _require_basis(quote)
    if quote.doc_type != Type.QUOTE:
        raise DocumentError("Eine Auftragsbestätigung entsteht aus einem Angebot")
    with transaction.atomic():
        confirmation = create_draft(
            quote.company,
            Type.ORDER_CONFIRMATION,
            quote.customer,
            issue_date=issue_date,
            service_date=quote.service_date,
            service_period=(
                (quote.service_period_start, quote.service_period_end) if quote.service_period_start else None
            ),
            created_by=created_by,
            origin=origin,
            idempotency_key=idempotency_key,
            language=quote.language,
            intro_text=quote.intro_text,
            closing_text=quote.closing_text,
            document_discount_percent=quote.document_discount_percent,
            parent=quote,
        )
        _copy_lines(quote, confirmation)
    return confirmation


def create_partial_invoice(
    quote: Document,
    *,
    issue_date: dt.date,
    percent: Decimal | None = None,
    net_amount: Decimal | None = None,
    service_date: dt.date | None = None,
    service_period: tuple[dt.date, dt.date] | None = None,
    created_by=None,
    origin: Document.Origin = Document.Origin.UI,
    idempotency_key: str = "",
) -> Document:
    """Abschlagsrechnung over a share of a finalized quote (one position per tax treatment)."""
    quote = _require_basis(quote)
    with transaction.atomic():
        partial = create_draft(
            quote.company,
            Type.PARTIAL_INVOICE,
            quote.customer,
            issue_date=issue_date,
            service_date=service_date,
            service_period=service_period,
            created_by=created_by,
            origin=origin,
            idempotency_key=idempotency_key,
            language=quote.language,
            parent=quote,
        )
        for line in _partial_lines(quote, percent=percent, net_amount=net_amount):
            add_line(partial, **line)
    return partial


def create_final_invoice(
    quote: Document,
    *,
    issue_date: dt.date,
    service_date: dt.date | None = None,
    service_period: tuple[dt.date, dt.date] | None = None,
    created_by=None,
    origin: Document.Origin = Document.Origin.UI,
    idempotency_key: str = "",
) -> Document:
    """Schlussrechnung over the whole quote, deducting every open Abschlagsrechnung for it."""
    quote = _require_basis(quote)
    with transaction.atomic():
        final = create_draft(
            quote.company,
            Type.FINAL_INVOICE,
            quote.customer,
            issue_date=issue_date,
            service_date=service_date,
            service_period=service_period,
            created_by=created_by,
            origin=origin,
            idempotency_key=idempotency_key,
            language=quote.language,
            intro_text=quote.intro_text,
            closing_text=quote.closing_text,
            document_discount_percent=quote.document_discount_percent,
            parent=quote,
        )
        _copy_lines(quote, final)
        partials = open_partials_for(quote)
        if not partials:
            raise DocumentError(
                f"Zu {quote.get_doc_type_display()} {quote.number} gibt es keine offene Abschlagsrechnung"
            )
        for partial in partials:
            add_deduction(final, partial)
    return final


# --- retention --------------------------------------------------------------------------------


def redact_party_snapshot(document: Document, role: PartySnapshot.Role) -> PartySnapshot:
    """Remove personal data after the retention period; the locked document keeps the hash."""
    snapshot = PartySnapshot.objects.get(document=document, role=role)
    snapshot.data = {"redacted": True}
    snapshot.redacted_at = timezone.now()
    snapshot.save(update_fields=["data", "redacted_at"])
    return snapshot
