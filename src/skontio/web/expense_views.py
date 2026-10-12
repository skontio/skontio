"""Ausgaben (with receipt photo) and the EÜR report."""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from skontio.archive.services import load
from skontio.attachments.services import MAX_BYTES
from skontio.audit.models import AuditEvent
from skontio.banking.models import BankTransaction
from skontio.banking.services import link_expense
from skontio.documents.renditions import de_amount
from skontio.expenses.models import Expense, PendingReceipt
from skontio.expenses.services import (
    ExpenseError,
    add_pending,
    cancel_expense,
    create_expense,
    discard_pending,
    hints,
    update_expense,
)
from skontio.reports.euer import euer, journal
from skontio.taxstatus.services import TaxStatusError, regime_for
from skontio.web.support import company_view

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
VAT_RATES = [("19", "19 %"), ("7", "7 %"), ("0", "0 % / keine USt ausgewiesen")]


class ExpenseForm(forms.Form):
    receipt = forms.FileField(
        label="Beleg (Foto oder PDF)",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*,application/pdf,.xml"}),
    )
    receipt_date = forms.DateField(label="Belegdatum", widget=DATE)
    supplier = forms.CharField(label="Lieferant", max_length=200)
    gross_amount = forms.DecimalField(
        label="Betrag (brutto, €)", min_value=Decimal("0.01"), decimal_places=2, localize=True
    )
    vat_rate = forms.TypedChoiceField(label="USt-Satz", choices=VAT_RATES, coerce=Decimal, initial="19")
    category = forms.ChoiceField(label="Kategorie", choices=Expense.Category.choices, initial=Expense.Category.OTHER)
    description = forms.CharField(label="Beschreibung", max_length=300, required=False)
    paid_on = forms.DateField(
        label="Bezahlt am",
        required=False,
        widget=DATE,
        help_text="Leer lassen, solange nicht bezahlt (zählt in der EÜR erst ab Zahlung).",
    )
    method = forms.ChoiceField(label="Zahlart", choices=Expense.Method.choices)
    supplier_country = forms.CharField(label="Land des Lieferanten", max_length=2, initial="DE")
    reverse_charge = forms.BooleanField(
        label="Rechnung ohne USt nach § 13b (z. B. Werbung/Software aus dem EU-Ausland)", required=False
    )
    note = forms.CharField(label="Notiz", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))


FIELDS = [name for name in ExpenseForm.base_fields if name != "receipt"]


def _year(request) -> int:
    try:
        return int(request.GET.get("jahr", ""))
    except ValueError:
        return timezone.localdate().year


def _years():
    this_year = timezone.localdate().year
    return range(this_year, this_year - 6, -1)


@company_view
def expense_list(request):
    year = _year(request)
    expenses = Expense.objects.filter(company=request.company, receipt_date__year=year)
    active = expenses.filter(cancelled_at__isnull=True)
    total = active.aggregate(total=Sum("gross_amount"))["total"] or Decimal("0")
    context = {
        "expenses": expenses,
        "year": year,
        "years": _years(),
        "total": de_amount(total),
        "missing_receipts": active.filter(receipt__isnull=True).count(),
        "unpaid": active.filter(paid_on__isnull=True).count(),
        "pending_count": PendingReceipt.objects.filter(company=request.company, expense__isnull=True).count(),
    }
    return render(request, "web/expenses/list.html", context)


def _save(request, expense: Expense | None):
    form = ExpenseForm(request.POST, request.FILES)
    if form.is_valid():
        data = {k: form.cleaned_data[k] for k in FIELDS}
        upload = form.cleaned_data["receipt"]
        receipt = {"receipt": upload.read(), "receipt_filename": upload.name} if upload else {}
        if expense is None and not upload and (pending := _pending(request)) is not None:
            receipt = {"pending": pending}
        try:
            if expense is None:
                expense = create_expense(request.company, user=request.user, **data, **receipt)
            else:
                expense = update_expense(expense, user=request.user, **data, **receipt)
        except ExpenseError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Ausgabe {expense.number} gespeichert.")
            return redirect(f"/ausgaben/{expense.pk}/"), form, expense
    return None, form, expense


def _bank_transaction(request):
    """``?bank=<pk>``: an outgoing bank transaction to prefill from and link to after saving."""
    pk = request.GET.get("bank") or request.POST.get("bank")
    if not pk or not str(pk).isdigit():
        return None
    tx = BankTransaction.objects.filter(pk=pk, company=request.company, amount__lt=0).first()
    return tx if tx is not None and not tx.done else None


def _pending(request) -> PendingReceipt | None:
    pk = request.GET.get("beleg") or request.POST.get("beleg")
    if not pk or not str(pk).isdigit():
        return None
    return PendingReceipt.objects.filter(pk=pk, company=request.company, expense__isnull=True).first()


@company_view
def expense_create(request):
    tx = _bank_transaction(request)
    if request.method == "POST":
        response, form, expense = _save(request, None)
        if response:
            if tx is not None:
                link_expense(tx, expense, user=request.user)
            return response
    else:
        today = timezone.localdate()
        initial = {"receipt_date": today, "paid_on": today}
        if tx is not None:
            initial |= {
                "receipt_date": tx.booked_on,
                "paid_on": tx.booked_on,
                "supplier": tx.counterparty,
                "gross_amount": -tx.amount,
                "description": tx.purpose[:300],
                "method": Expense.Method.BANK,
            }
        form = ExpenseForm(initial=initial)
    context = {"form": form, "expense": None, "bank": tx, "pending": _pending(request)}
    return render(request, "web/expenses/form.html", context)


def _regime(company, day):
    try:
        return regime_for(company, day)
    except TaxStatusError:
        return None


@company_view
def expense_detail(request, pk: int):
    expense = get_object_or_404(Expense, pk=pk, company=request.company)
    if request.method == "POST":
        if expense.cancelled_at:
            return HttpResponse("Diese Ausgabe ist storniert.", status=409)
        response, form, _ = _save(request, expense)
        if response:
            return response
    else:
        initial = {f: getattr(expense, f) for f in FIELDS} | {"vat_rate": f"{expense.vat_rate.normalize():f}"}
        form = ExpenseForm(initial=initial)
    history = AuditEvent.objects.filter(entity="expense", entity_id=str(expense.pk)).select_related("actor")
    context = {
        "form": form,
        "expense": expense,
        "hints": hints(expense, _regime(request.company, expense.receipt_date)),
        "history": history,
    }
    return render(request, "web/expenses/form.html", context)


@company_view
def expense_receipt(request, pk: int):
    expense = get_object_or_404(Expense, pk=pk, company=request.company)
    if not expense.receipt_id:
        raise Http404
    inline = expense.receipt.mime in {"application/pdf", "image/png", "image/jpeg", "image/webp"}
    response = FileResponse(
        io.BytesIO(load(expense.receipt)),
        as_attachment=not inline,
        filename=expense.receipt_filename or expense.number,
        content_type=expense.receipt.mime,
    )
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"  # uploaded content
    return response


@require_POST
@company_view
def expense_cancel(request, pk: int):
    expense = get_object_or_404(Expense, pk=pk, company=request.company)
    try:
        cancel_expense(expense, reason=request.POST.get("reason", ""), user=request.user)
    except ExpenseError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Ausgabe {expense.number} storniert.")
    return redirect(f"/ausgaben/{expense.pk}/")


@company_view
def euer_report(request):
    year = _year(request)
    report = euer(request.company, year)
    context = {
        "report": report,
        "year": year,
        "years": _years(),
        "income": [(line.label, de_amount(line.amount)) for line in report.income],
        "expenses": [(line.label, de_amount(line.amount)) for line in report.expenses],
        "income_total": de_amount(report.income_total),
        "expense_total": de_amount(report.expense_total),
        "profit": de_amount(report.profit),
        "loss": report.profit < 0,
    }
    return render(request, "web/reports/euer.html", context)


def _csv_safe(value: str) -> str:
    """Spreadsheet formula injection: a leading =, +, - or @ would be executed by Excel/LibreOffice."""
    return "'" + value if value[:1] in {"=", "+", "-", "@", "\t", "\r"} else value


@company_view
def euer_csv(request):
    year = _year(request)
    labels = dict(Expense.Category.choices) | {
        "small_business": "Einnahmen Kleinunternehmer",
        "taxable": "Einnahmen steuerpflichtig",
        "tax_free": "Einnahmen steuerfrei",
    }
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Datum", "Art", "Beleg", "Partner", "Zuordnung", "Netto", "USt", "Brutto", "Abziehbar"])
    for e in journal(request.company, year):
        deductible = de_amount(e.deductible) if e.kind == "Ausgabe" else ""
        writer.writerow(
            [
                e.date.isoformat(),
                e.kind,
                e.number,
                _csv_safe(e.counterparty),
                labels.get(e.line, e.line),
                de_amount(e.net),
                de_amount(e.vat),
                de_amount(e.gross),
                deductible,
            ]
        )
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="euer-{year}.csv"'
    return response


# --- Belegeingang ------------------------------------------------------------------------------


@company_view
def receipt_inbox(request):
    if request.method == "POST":
        files = request.FILES.getlist("files")
        added, errors = _add_files(request, files, note="")
        if added:
            messages.success(request, f"{added} Beleg(e) im Belegeingang.")
        for error in errors:
            messages.error(request, error)
        return redirect("receipt_inbox")
    pending = PendingReceipt.objects.filter(company=request.company, expense__isnull=True).select_related("blob")
    return render(request, "web/expenses/inbox.html", {"pending": pending})


def _add_files(request, files, *, note: str) -> tuple[int, list[str]]:
    added, errors = 0, []
    for upload in files[:20]:
        if upload.size > MAX_BYTES:
            errors.append(f"{upload.name}: größer als 20 MB")
            continue
        try:
            add_pending(request.company, upload.read(), filename=upload.name, note=note, user=request.user)
        except ExpenseError as exc:
            errors.append(f"{upload.name}: {exc}")
        else:
            added += 1
    return added, errors


@csrf_exempt  # Web Share Target: the OS posts without a CSRF token; the Lax session cookie keeps other sites out
@require_POST
@company_view
def receipt_share(request):
    note = " ".join(request.POST.get(k, "") for k in ("title", "text", "url")).strip()
    added, errors = _add_files(request, request.FILES.getlist("receipt"), note=note)
    if added:
        messages.success(request, f"{added} Beleg(e) empfangen – jetzt erfassen oder später.")
    for error in errors:
        messages.error(request, error)
    if not added and not errors:
        messages.error(request, "Es wurde keine Datei geteilt.")
    return redirect("receipt_inbox")


@company_view
def receipt_file(request, pk: int):
    pending = get_object_or_404(PendingReceipt, pk=pk, company=request.company)
    inline = pending.blob.mime in {"application/pdf", "image/png", "image/jpeg", "image/webp"}
    response = FileResponse(
        io.BytesIO(load(pending.blob)),
        as_attachment=not inline,
        filename=pending.filename,
        content_type=pending.blob.mime,
    )
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    return response


@require_POST
@company_view
def receipt_discard(request, pk: int):
    pending = get_object_or_404(PendingReceipt, pk=pk, company=request.company)
    try:
        discard_pending(pending, user=request.user)
    except ExpenseError as exc:
        messages.error(request, str(exc))
    return redirect("receipt_inbox")
