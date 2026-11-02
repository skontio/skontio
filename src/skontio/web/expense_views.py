"""Ausgaben (with receipt photo) and the EÜR report."""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from skontio.archive.services import load
from skontio.assistant.providers import ProviderError
from skontio.assistant.services import current_config, get_provider
from skontio.attachments.services import MAX_BYTES
from skontio.audit.models import AuditEvent
from skontio.audit.services import record
from skontio.banking.models import BankRule, BankTransaction
from skontio.banking.services import link_expense
from skontio.crm.models import Customer
from skontio.crm.services import SUPPLIER_ROLES
from skontio.documents.renditions import de_amount
from skontio.expenses import recognition
from skontio.expenses import recurring as recurring_expenses
from skontio.expenses.models import Expense, PendingReceipt, ReceiptMail, RecurringExpense
from skontio.expenses.services import (
    ExpenseError,
    add_pending,
    cancel_expense,
    create_expense,
    discard_pending,
    hints,
    update_expense,
)
from skontio.reports import ustva
from skontio.reports.euer import euer, journal
from skontio.reports.zm import KINDS as ZM_KINDS
from skontio.reports.zm import zm_report
from skontio.taxstatus.models import VatReturn
from skontio.taxstatus.services import TaxStatusError, regime_for
from skontio.web.csvexport import csv_safe
from skontio.web.document_views import _iso_date
from skontio.web.support import company_view, full_access

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
VAT_RATES = [("19", "19 %"), ("7", "7 %"), ("0", "0 % / keine USt ausgewiesen")]


class ExpenseForm(forms.Form):
    receipt = forms.FileField(
        label="Beleg (Foto oder PDF)",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*,application/pdf,.xml"}),
    )
    receipt_date = forms.DateField(label="Belegdatum", widget=DATE)
    supplier_contact = forms.ModelChoiceField(
        queryset=Customer.objects.none(), label="Lieferant aus den Kontakten", required=False,
        empty_label="– kein Kontakt –",
    )  # fmt: skip
    supplier = forms.CharField(label="Lieferant", max_length=200, required=False, help_text="wie auf dem Beleg")
    gross_amount = forms.DecimalField(
        label="Betrag (brutto, €)", min_value=Decimal("0.01"), max_digits=12, decimal_places=2, localize=True
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

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["supplier_contact"].queryset = Customer.objects.filter(
                company=company, role__in=SUPPLIER_ROLES, archived_at__isnull=True
            )

    def clean(self):
        data = super().clean()
        contact = data.get("supplier_contact")
        if not data.get("supplier") and contact is None:
            self.add_error("supplier", "Bitte den Lieferanten angeben oder einen Kontakt wählen.")
        elif contact is not None and not data.get("supplier"):
            data["supplier"] = contact.name
        return data


FIELDS = [name for name in ExpenseForm.base_fields if name != "receipt"]


def _year(request) -> int:
    """?jahr= (also from a POST), only 1900–9999 – anything else is the current year."""
    raw = request.GET.get("jahr") or request.POST.get("jahr") or ""
    if len(raw) == 4 and raw.isascii() and raw.isdigit() and int(raw) >= 1900:
        return int(raw)
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
        "suggestions": recurring_expenses.due_suggestions(request.company),
        "templates": [
            (t, recurring_expenses.next_due(t)) for t in RecurringExpense.objects.filter(company=request.company)
        ],
    }
    return render(request, "web/expenses/list.html", context)


def _save(request, expense: Expense | None):
    form = ExpenseForm(request.POST, request.FILES, company=request.company)
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


def _rule_prefill(request, tx: BankTransaction) -> dict:
    """``?regel=<pk>``: category and supplier of a matching bank rule."""
    raw = request.GET.get("regel", "")
    if not (raw.isascii() and raw.isdigit() and len(raw) < 19):
        return {}
    rule = BankRule.objects.filter(pk=raw, company=request.company, action=BankRule.Action.EXPENSE).first()
    if rule is None or not rule.matches(tx):
        return {}
    return {"category": rule.category} | ({"supplier": rule.supplier} if rule.supplier else {})


def _template(request) -> tuple[RecurringExpense, dt.date] | None:
    """``?vorlage=<pk>&faellig=<date>``: a due recurring expense to prefill from; only its open period."""
    pk = request.GET.get("vorlage") or request.POST.get("vorlage") or ""
    due = _iso_date(request.GET.get("faellig") or request.POST.get("faellig") or "")
    if not (pk.isascii() and pk.isdigit() and len(pk) < 19) or due is None:
        return None
    template = RecurringExpense.objects.filter(pk=pk, company=request.company, active=True).first()
    if template is None or recurring_expenses.next_due(template) != due:
        return None
    return template, due


def _pending(request) -> PendingReceipt | None:
    pk = request.GET.get("beleg") or request.POST.get("beleg")
    if not pk or not str(pk).isdigit():
        return None
    return PendingReceipt.objects.filter(pk=pk, company=request.company, expense__isnull=True).first()


@company_view
def expense_create(request):
    tx = _bank_transaction(request)
    template = _template(request)
    if request.method == "POST":
        response, form, expense = _save(request, None)
        if response:
            if tx is not None:
                link_expense(tx, expense, user=request.user)
            if template is not None:
                recurring_expenses.mark_done(template[0], template[1], user=request.user, action="taken")
            return response
    else:
        today = timezone.localdate()
        initial = {"receipt_date": today, "paid_on": today}
        if template is not None:
            initial |= recurring_expenses.prefill(*template)
            initial["vat_rate"] = f"{template[0].vat_rate.normalize():f}"
        if tx is not None:
            initial |= {
                "receipt_date": tx.booked_on,
                "paid_on": tx.booked_on,
                "supplier": tx.counterparty,
                "gross_amount": -tx.amount,
                "description": tx.purpose[:300],
                "method": Expense.Method.BANK,
            }
            initial |= _rule_prefill(request, tx)
        pending = _pending(request)
        if pending is not None and pending.recognized and tx is None and template is None:
            initial |= recognition.initial_from(pending.recognized)
        form = ExpenseForm(initial=initial, company=request.company)
    context = {"form": form, "expense": None, "bank": tx, "pending": _pending(request), "template": template,
               "ai_available": _ai_available()}  # fmt: skip
    return render(request, "web/expenses/form.html", context)


@require_POST
@company_view
def recurring_from_expense(request, pk: int):
    expense = get_object_or_404(Expense, pk=pk, company=request.company)
    interval = request.POST.get("interval", "")
    try:
        template = recurring_expenses.from_expense(
            expense, interval=int(interval) if interval.isdigit() and len(interval) < 3 else 0, user=request.user
        )
    except ExpenseError as exc:
        messages.error(request, str(exc))
        return redirect(f"/ausgaben/{expense.pk}/")
    messages.success(request, f"„{template.name}“ wird ab {template.start_date:%d.%m.%Y} vorgeschlagen.")
    return redirect("/ausgaben/")


@require_POST
@company_view
def recurring_skip(request, pk: int):
    template = get_object_or_404(RecurringExpense, pk=pk, company=request.company)
    due = _iso_date(request.POST.get("faellig", ""))
    if due is None or not recurring_expenses.mark_done(template, due, user=request.user, action="skipped"):
        messages.error(request, "Dieser Termin ist schon erledigt.")
    return redirect("/ausgaben/")


@require_POST
@company_view
def recurring_toggle(request, pk: int):
    template = get_object_or_404(RecurringExpense, pk=pk, company=request.company)
    recurring_expenses.toggle(template, user=request.user)
    return redirect("/ausgaben/")


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
        form = ExpenseForm(initial=initial, company=request.company)
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
@full_access
def expense_cancel(request, pk: int):
    expense = get_object_or_404(Expense, pk=pk, company=request.company)
    try:
        cancel_expense(expense, reason=request.POST.get("reason", ""), user=request.user)
    except ExpenseError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Ausgabe {expense.number} storniert.")
    return redirect(f"/ausgaben/{expense.pk}/")


def _range(request):
    """(year, start, end, custom): ?von=&bis= wins over ?jahr= when both bounds are valid."""
    year = _year(request)
    start, end = _iso_date(request.GET.get("von", "")), _iso_date(request.GET.get("bis", ""))
    if start and end and start <= end:
        return start.year, start, end, True
    return year, dt.date(year, 1, 1), dt.date(year, 12, 31), False


@company_view
def euer_report(request):
    year, start, end, custom = _range(request)
    report = euer(request.company, start=start, end=end)
    context = {
        "report": report,
        "year": year,
        "years": _years(),
        "start": start,
        "end": end,
        "custom": custom,
        "range_query": f"von={start.isoformat()}&bis={end.isoformat()}" if custom else f"jahr={year}",
        "income": [(line.label, de_amount(line.amount)) for line in report.income],
        "expenses": [(line.label, de_amount(line.amount)) for line in report.expenses],
        "income_total": de_amount(report.income_total),
        "expense_total": de_amount(report.expense_total),
        "profit": de_amount(report.profit),
        "loss": report.profit < 0,
    }
    return render(request, "web/reports/euer.html", context)


_csv_safe = csv_safe


@company_view
def euer_csv(request):
    year, start, end, custom = _range(request)
    labels = dict(Expense.Category.choices) | {
        "small_business": "Einnahmen Kleinunternehmer",
        "taxable": "Einnahmen steuerpflichtig",
        "tax_free": "Einnahmen steuerfrei",
    }
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Datum", "Art", "Beleg", "Partner", "Zuordnung", "Netto", "USt", "Brutto", "Abziehbar"])
    for e in journal(request.company, start=start, end=end):
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
    name = f"euer-{start.isoformat()}-bis-{end.isoformat()}" if custom else f"euer-{year}"
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    return response


# --- Belegeingang ------------------------------------------------------------------------------


def _ai_available() -> bool:
    return current_config()["kind"] not in ("", "none")


@require_POST
@company_view
def receipt_recognize(request, pk: int):
    """Belegerkennung: let the language model suggest the fields, then open the prefilled form."""
    pending = get_object_or_404(PendingReceipt, pk=pk, company=request.company, expense__isnull=True)
    try:
        provider = get_provider()
    except ProviderError as exc:
        messages.error(request, f"KI-Anbieter nicht nutzbar: {exc}")
        provider = False
    if provider is not False:
        try:
            result = recognition.recognize(pending, provider=provider)
        except ExpenseError as exc:
            messages.error(request, str(exc))
        else:
            for warning in result["warnings"]:
                messages.warning(request, warning)
            messages.info(request, "Vorschlag der KI übernommen – bitte alle Felder prüfen, bevor du speicherst.")
    return redirect(f"/ausgaben/neu/?beleg={pending.pk}")


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
    context = {
        "pending": pending,
        "mailbox_address": settings.RECEIPT_MAILBOX_ADDRESS,
        "mails": ReceiptMail.objects.filter(company=request.company)[:20],
        "ai_available": _ai_available(),
    }
    return render(request, "web/expenses/inbox.html", context)


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


# --- Umsatzsteuer-Voranmeldung -----------------------------------------------------------------


def _elster(value: Decimal) -> str:
    """Format for typing into Mein ELSTER: comma, no thousands separator."""
    return f"{value:f}".replace(".", ",")


def _period(request, company):
    today = timezone.localdate()
    year = _year(request)
    kind = company.vat_period
    choices = ustva.periods(year, kind)
    code = request.GET.get("zeitraum") or request.POST.get("zeitraum")
    if code:
        try:
            return ustva.parse_period(year, code), choices
        except ValueError:
            pass
    past = [p for p in choices if p.end < today] or choices[:1]
    return past[-1], choices


@company_view
def ustva_report(request):
    period, choices = _period(request, request.company)
    try:
        report = ustva.vat_report(request.company, period)
    except ustva.SollNotSupported as exc:
        return render(request, "web/reports/ustva.html", {"error": str(exc), "period": period, "choices": choices})
    vat_return = ustva.submitted(request.company, period)
    context = {
        "period": period,
        "choices": choices,
        "years": _years(),
        "report": report,
        "rows": [
            (kz, label, de_amount(value, 0 if kz in ustva.BASES else 2), _elster(value))
            for kz, label, value in report.rows()
        ],
        "payable": report.values["83"],
        "payable_text": de_amount(abs(report.values["83"])),
        "deadline": None if period.annual else period.deadline(request.company.vat_extension),
        "prepayments": _prepayments(request.company, period) if period.annual else None,
        "vat_return": vat_return,
        "differences": ustva.differences(report, vat_return) if vat_return else {},
    }
    return render(request, "web/reports/ustva.html", context)


@company_view
def zm(request):
    """Zusammenfassende Meldung: quarters by default, months on request (?zeitraum=M01…)."""
    year = _year(request)
    code = request.GET.get("zeitraum", "")
    choices = ustva.periods(year, "quarterly")
    try:
        period = ustva.parse_period(year, code) if code and code != "J" else None
    except ValueError:
        period = None
    if period is None:
        today = timezone.localdate()
        period = ([p for p in choices if p.end < today] or choices[:1])[-1]
    report = zm_report(request.company, period)
    context = {
        "report": report,
        "period": period,
        "choices": choices,
        "months": ustva.periods(year, "monthly"),
        "years": _years(),
        "rows": [(row, ZM_KINDS[row.kind]) for row in report.rows],
        "no_vat_id": not request.company.vat_id,
    }
    return render(request, "web/reports/zm.html", context)


def _prepayments(company, period):
    """Sum of Kz 83 of the Voranmeldungen marked as submitted for that year (annual view only)."""
    returns = VatReturn.objects.filter(company=company, year=period.year).exclude(period="J")
    total = sum((Decimal(r.values.get("83", "0")) for r in returns), Decimal("0"))
    return {"count": returns.count(), "total": de_amount(total), "raw": total}


@require_POST
@company_view
@full_access
def ustva_submit(request):
    period, _ = _period(request, request.company)
    if period.annual:
        return HttpResponse("Die Jahresübersicht ist keine Voranmeldung.", status=400)
    report = ustva.vat_report(request.company, period)
    VatReturn.objects.update_or_create(
        company=request.company,
        year=period.year,
        period=period.code,
        defaults={"values": report.frozen(), "submitted_by": request.user},
    )
    record(
        "vat_return.submitted",
        actor=request.user,
        company=request.company,
        data={"period": f"{period.year}-{period.code}", "values": report.frozen()},
    )
    messages.success(request, f"Voranmeldung {period.label} als übermittelt vermerkt.")
    return redirect(f"/auswertungen/ustva/?jahr={period.year}&zeitraum={period.code}")
