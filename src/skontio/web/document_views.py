"""Document editor and actions. All business rules live in the services; views only translate."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from skontio.archive.services import load
from skontio.documents.models import BASIS_TYPES, INVOICE_TYPES, Document, DocumentLine, Rendition
from skontio.documents.renditions import UNIT_LABELS, de_amount, de_percent, de_quantity
from skontio.documents.services import (
    DocumentError,
    DocumentLocked,
    add_deduction,
    add_line,
    add_line_from_item,
    cancel_invoice,
    confirm_ai_review,
    convert_quote_to_invoice,
    create_draft,
    create_final_invoice,
    create_order_confirmation,
    create_partial_invoice,
    eligible_partial_invoices,
    finalize,
    open_partials_for,
    prepaid_total,
    preview_pdf,
    preview_totals,
    remove_deduction,
)
from skontio.mail.services import MailError, default_message, send_document
from skontio.payments.services import PaymentError, open_amount, payment_status, record_payment
from skontio.tax.engine import TaxTreatment
from skontio.web.forms import (
    CancelForm,
    ConvertForm,
    FollowUpForm,
    HeaderForm,
    LineForm,
    NewDocumentForm,
    PartialForm,
    PaymentForm,
    SendForm,
)
from skontio.web.support import company_view, is_htmx

Type = Document.Type
TYPE_FILTERS = {
    "invoice": INVOICE_TYPES,
    "quote": {Type.QUOTE},
    "order_confirmation": {Type.ORDER_CONFIRMATION},
    "cancellation": {Type.CANCELLATION},
}
FILE_SUFFIX = {
    Rendition.Kind.PDF: ".pdf",
    Rendition.Kind.ZUGFERD_PDF: ".pdf",
    Rendition.Kind.XRECHNUNG_XML: "-xrechnung.xml",
}
PAYMENT_LABELS = {"open": "offen", "partial": "teilweise bezahlt", "paid": "bezahlt"}
HEADER_FIELDS = [
    "issue_date",
    "service_date",
    "service_period_start",
    "service_period_end",
    "due_date",
    "valid_until",
    "document_discount_percent",
    "intro_text",
    "closing_text",
    "language",
    "template",
]


def _document(request, pk: int) -> Document:
    return get_object_or_404(Document.objects.select_related("customer", "parent"), pk=pk, company=request.company)


def _locked() -> HttpResponse:
    return HttpResponse("Dieses Dokument ist festgeschrieben und kann nicht mehr geändert werden.", status=409)


def _lines_context(request, doc: Document, line_form: LineForm | None = None, error: str = "") -> dict:
    lines = list(doc.lines.all())
    totals, totals_error = None, ""
    if lines:
        try:
            totals = preview_totals(doc)
        except DocumentError as exc:
            totals_error = str(exc)
    rows = [
        {
            "line": line,
            "quantity": de_quantity(line.quantity),
            "unit": UNIT_LABELS.get(line.unit_code, line.unit_code),
            "unit_price": de_amount(line.unit_price),
            "discount": de_percent(line.discount_percent) if line.discount_percent else "",
            "net": de_amount(totals.lines[index].net_amount) if totals else "",
        }
        for index, line in enumerate(lines)
    ]
    breakdown = (
        [{"rate": de_percent(b.rate), "tax": de_amount(b.tax), "reason": b.exemption_reason} for b in totals.breakdown]
        if totals
        else []
    )
    fmt = (
        {
            "line_total": de_amount(totals.line_total),
            "allowance_total": de_amount(totals.allowance_total),
            "tax_total": de_amount(totals.tax_total),
            "grand_total": de_amount(totals.grand_total),
        }
        if totals
        else {}
    )
    return {
        **_attachment_context(doc),
        "doc": doc,
        "rows": rows,
        "totals": totals,
        "fmt": fmt,
        "breakdown": breakdown,
        "notes": list(dict.fromkeys(b["reason"] for b in breakdown if b["reason"])),
        "totals_error": totals_error,
        "line_form": line_form or LineForm(company=request.company, initial={"quantity": 1}),
        "line_error": error,
        **_deduction_context(doc, totals),
    }


def _deduction_context(doc: Document, totals=None) -> dict:
    """Schlussrechnung: deducted partial invoices, what is left to pay and what could still be deducted."""
    if doc.doc_type != Type.FINAL_INVOICE and not doc.deductions.exists():
        return {}  # only Schlussrechnungen and their Stornos carry deductions
    grouped: dict[int, dict] = {}
    for d in doc.deductions.select_related("source"):
        entry = grouped.setdefault(d.source_id, {"source": d.source, "net": 0, "tax": 0, "gross": 0})
        entry["net"] += d.net_amount
        entry["tax"] += d.tax_amount
        entry["gross"] += d.gross_amount
    gross = totals.grand_total if totals is not None else doc.gross_total
    prepaid = prepaid_total(doc)
    return {
        "deductions": [
            {"source": e["source"], **{k: de_amount(e[k]) for k in ("net", "tax", "gross")}} for e in grouped.values()
        ],
        "prepaid": de_amount(prepaid),
        "due": de_amount(gross - prepaid) if gross is not None else "",
        "eligible_partials": (
            eligible_partial_invoices(doc.company, doc.customer, exclude=doc)
            if doc.status == Document.Status.DRAFT
            else []
        ),
    }


def _attachment_context(doc: Document) -> dict:
    return {"attachments": doc.attachments.select_related("blob"), "attach_document": doc}


def _finalized_context(doc: Document, **extra) -> dict:
    context = {
        **_attachment_context(doc),
        "doc": doc,
        "renditions": list(doc.renditions.all()),
        "lines": [
            {
                "line": line,
                "quantity": de_quantity(line.quantity),
                "unit": UNIT_LABELS.get(line.unit_code, line.unit_code),
                "unit_price": de_amount(line.unit_price),
                "net": de_amount(line.net_amount),
                "rate": de_percent(line.tax_rate),
            }
            for line in doc.lines.all()
        ],
        "gross": de_amount(doc.gross_total),
        "net": de_amount(doc.net_total),
        "tax": de_amount(doc.tax_total),
        "children": doc.children.all(),
        "mail_logs": doc.mail_logs.all(),
        "send_form": _send_form(doc),
    }
    context |= _deduction_context(doc)
    if doc.doc_type in INVOICE_TYPES:
        context |= {
            "cancelled": doc.children.filter(doc_type=Type.CANCELLATION).exists(),
            "open_amount": de_amount(open_amount(doc)),
            "payment_status": PAYMENT_LABELS[payment_status(doc)],
            "payments": doc.payments.all(),
            "payment_form": PaymentForm(initial={"amount": open_amount(doc)}),
            "cancel_form": CancelForm(),
        }
    if doc.doc_type in BASIS_TYPES:
        context["convert_form"] = ConvertForm()
        context["partial_form"] = PartialForm(initial={"percent": 30})
        context["open_partials"] = open_partials_for(doc)
        context["can_confirm_order"] = doc.doc_type == Type.QUOTE
        context["follow_up_form"] = FollowUpForm(
            initial={
                "title": f"Angebot {doc.number} nachfassen",
                "due_on": timezone.localdate() + dt.timedelta(days=14),
            }
        )
    return context | extra


@company_view
def document_list(request):
    documents = Document.objects.filter(company=request.company).select_related("customer")
    doc_type = request.GET.get("typ", "")
    if doc_type in TYPE_FILTERS:
        documents = documents.filter(doc_type__in=TYPE_FILTERS[doc_type])
    return render(request, "web/documents/list.html", {"documents": documents[:200], "typ": doc_type})


@company_view
def document_new(request):
    customer = request.GET.get("kunde", "")
    initial = {"doc_type": request.GET.get("typ", "invoice"), "customer": int(customer) if customer.isdigit() else None}
    form = NewDocumentForm(request.POST or None, company=request.company, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        doc = create_draft(
            request.company,
            Type(data["doc_type"]),
            data["customer"],
            issue_date=data["issue_date"],
            service_date=data["service_date"],
            created_by=request.user,
        )
        return redirect(f"/dokumente/{doc.pk}/")
    return render(request, "web/documents/new.html", {"form": form})


@company_view
def document_detail(request, pk: int):
    doc = _document(request, pk)
    if doc.status != Document.Status.DRAFT:
        if request.method == "POST":
            return _locked()
        return render(request, "web/documents/finalized.html", _finalized_context(doc))
    initial = {f: getattr(doc, f) for f in HEADER_FIELDS}
    header = HeaderForm(request.POST or None, initial=initial, company=request.company)
    if request.method == "POST" and header.is_valid():
        data = header.cleaned_data
        for field in HEADER_FIELDS:
            if field in data:  # "template" only exists when the company has templates
                setattr(doc, field, data[field])
        doc.document_discount_percent = data["document_discount_percent"] or Decimal("0")
        doc.language = data["language"] or header.initial["language"]
        doc.save()
        return redirect(f"/dokumente/{doc.pk}/")
    return render(request, "web/documents/draft.html", {"header": header, **_lines_context(request, doc)})


@require_POST
@company_view
def line_add(request, pk: int):
    doc = _document(request, pk)
    if doc.status != Document.Status.DRAFT:
        return _locked()
    form = LineForm(request.POST, company=request.company)
    error = ""
    if form.is_valid():
        data = form.cleaned_data
        discount = data["discount_percent"] or Decimal("0")
        try:
            if data["item"]:
                add_line_from_item(doc, data["item"], quantity=data["quantity"], discount_percent=discount)
            else:
                add_line(
                    doc,
                    text=data["text"],
                    quantity=data["quantity"],
                    unit_code=data["unit_code"].code if data["unit_code"] else "C62",
                    unit_price=data["unit_price"],
                    discount_percent=discount,
                    tax_treatment=TaxTreatment(data["tax_treatment"] or TaxTreatment.STANDARD),
                )
            form = None
        except DocumentError as exc:
            error = str(exc)
    if form is None and not is_htmx(request):
        return redirect(f"/dokumente/{doc.pk}/")
    return render(request, "web/documents/_lines.html", _lines_context(request, doc, form, error))


@require_POST
@company_view
def line_remove(request, pk: int, line_pk: int):
    doc = _document(request, pk)
    if doc.status != Document.Status.DRAFT:
        return _locked()
    get_object_or_404(DocumentLine, pk=line_pk, document=doc).delete()
    if is_htmx(request):
        return render(request, "web/documents/_lines.html", _lines_context(request, doc))
    return redirect(f"/dokumente/{doc.pk}/")


@require_POST
@company_view
def document_finalize(request, pk: int):
    doc = _document(request, pk)
    try:
        finalize(doc)
    except DocumentLocked:
        return _locked()
    except DocumentError as exc:
        doc.refresh_from_db()
        header = HeaderForm(initial={f: getattr(doc, f) for f in HEADER_FIELDS}, company=request.company)
        context = {"header": header, "finalize_error": str(exc), **_lines_context(request, doc)}
        return render(request, "web/documents/draft.html", context)
    return redirect(f"/dokumente/{doc.pk}/")


@company_view
def document_file(request, pk: int, kind: str):
    doc = _document(request, pk)
    if kind not in Rendition.Kind.values:
        raise Http404
    rendition = get_object_or_404(Rendition, document=doc, kind=kind)
    filename = f"{doc.number}{FILE_SUFFIX[Rendition.Kind(kind)]}"
    return FileResponse(
        io.BytesIO(load(rendition.blob)), as_attachment=True, filename=filename, content_type=rendition.blob.mime
    )


@company_view
def document_preview(request, pk: int):
    """Draft as PDF (marked as draft) – to check layout, texts and totals before finalizing."""
    doc = _document(request, pk)
    if doc.status != Document.Status.DRAFT:
        return redirect(f"/dokumente/{doc.pk}/")
    try:
        pdf = preview_pdf(doc)
    except DocumentError as exc:
        return HttpResponse(f"Keine Vorschau möglich: {exc}", status=409, content_type="text/plain; charset=utf-8")
    response = FileResponse(io.BytesIO(pdf), filename="entwurf.pdf", content_type="application/pdf")
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@company_view
def document_payment(request, pk: int):
    doc = _document(request, pk)
    form = PaymentForm(request.POST)
    if form.is_valid():
        try:
            record_payment(doc, created_by=request.user, **form.cleaned_data)
            return redirect(f"/dokumente/{doc.pk}/")
        except PaymentError as exc:
            form.add_error(None, str(exc))
    return render(request, "web/documents/finalized.html", _finalized_context(doc, payment_form=form))


@require_POST
@company_view
def document_cancel(request, pk: int):
    doc = _document(request, pk)
    form = CancelForm(request.POST)
    if form.is_valid():
        try:
            storno = cancel_invoice(
                doc, issue_date=timezone.localdate(), reason=form.cleaned_data["reason"], created_by=request.user
            )
            return redirect(f"/dokumente/{storno.pk}/")
        except DocumentError as exc:
            form.add_error(None, str(exc))
    return render(request, "web/documents/finalized.html", _finalized_context(doc, cancel_form=form))


@require_POST
@company_view
def document_convert(request, pk: int):
    doc = _document(request, pk)
    form = ConvertForm(request.POST)
    if form.is_valid():
        try:
            invoice = convert_quote_to_invoice(
                doc,
                issue_date=timezone.localdate(),
                service_date=form.cleaned_data["service_date"],
                created_by=request.user,
            )
            return redirect(f"/dokumente/{invoice.pk}/")
        except DocumentError as exc:
            form.add_error(None, str(exc))
    return render(request, "web/documents/finalized.html", _finalized_context(doc, convert_form=form))


def _send_form(doc: Document, data=None) -> SendForm:
    subject, body = default_message(doc)
    recipient = doc.customer.email if doc.customer else ""
    return SendForm(data, initial={"to": recipient, "subject": subject, "body": body})


@require_POST
@company_view
def document_send(request, pk: int):
    doc = _document(request, pk)
    if doc.status != Document.Status.FINALIZED:
        return HttpResponse("Nur festgeschriebene Dokumente können versendet werden.", status=409)
    form = _send_form(doc, request.POST)
    if form.is_valid():
        data = form.cleaned_data
        try:
            send_document(
                doc,
                to=SendForm.split(data["to"]),
                cc=SendForm.split(data["cc"]),
                subject=data["subject"],
                body=data["body"],
                sent_by=request.user,
            )
            return redirect(f"/dokumente/{doc.pk}/")
        except MailError as exc:
            form.add_error(None, str(exc))
    return render(request, "web/documents/finalized.html", _finalized_context(doc, send_form=form))


@require_POST
@company_view
def document_review(request, pk: int):
    doc = _document(request, pk)
    try:
        confirm_ai_review(doc, request.user)
    except DocumentLocked:
        return _locked()
    return redirect(f"/dokumente/{doc.pk}/")


@require_POST
@company_view
def document_delete(request, pk: int):
    doc = _document(request, pk)
    if doc.status != Document.Status.DRAFT:
        return _locked()
    doc.delete()
    return redirect("/dokumente/")


@require_POST
@company_view
def document_order_confirmation(request, pk: int):
    quote = _document(request, pk)
    try:
        confirmation = create_order_confirmation(quote, issue_date=timezone.localdate(), created_by=request.user)
    except DocumentError as exc:
        return HttpResponse(str(exc), status=409)
    return redirect(f"/dokumente/{confirmation.pk}/")


@require_POST
@company_view
def document_partial(request, pk: int):
    quote = _document(request, pk)
    form = PartialForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        try:
            partial = create_partial_invoice(
                quote,
                issue_date=timezone.localdate(),
                percent=data["percent"],
                net_amount=data["net_amount"],
                service_date=data["service_date"],
                created_by=request.user,
            )
            return redirect(f"/dokumente/{partial.pk}/")
        except DocumentError as exc:
            form.add_error(None, str(exc))
    return render(request, "web/documents/finalized.html", _finalized_context(quote, partial_form=form))


@require_POST
@company_view
def document_final(request, pk: int):
    quote = _document(request, pk)
    form = ConvertForm(request.POST)
    if form.is_valid():
        try:
            final = create_final_invoice(
                quote,
                issue_date=timezone.localdate(),
                service_date=form.cleaned_data["service_date"],
                created_by=request.user,
            )
            return redirect(f"/dokumente/{final.pk}/")
        except DocumentError as exc:
            form.add_error(None, str(exc))
    return render(request, "web/documents/finalized.html", _finalized_context(quote, convert_form=form))


@require_POST
@company_view
def deduction_add(request, pk: int):
    doc = _document(request, pk)
    partial = get_object_or_404(Document, pk=request.POST.get("partial", 0), company=request.company)
    try:
        add_deduction(doc, partial)
    except (DocumentError, DocumentLocked) as exc:
        return HttpResponse(str(exc), status=409)
    return redirect(f"/dokumente/{doc.pk}/")


@require_POST
@company_view
def deduction_remove(request, pk: int, partial_pk: int):
    doc = _document(request, pk)
    try:
        remove_deduction(doc, get_object_or_404(Document, pk=partial_pk, company=request.company))
    except DocumentLocked:
        return _locked()
    return redirect(f"/dokumente/{doc.pk}/")
