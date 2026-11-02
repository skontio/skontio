"""Sonstige Einnahmen ohne Rechnung (logic in skontio.expenses.income)."""

from __future__ import annotations

import io
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from skontio.archive.services import load
from skontio.expenses import income as other_income
from skontio.expenses.models import Expense, OtherIncome
from skontio.web.expense_views import DATE, _year, _years
from skontio.web.support import company_view, full_access

RATES = [("0", "0 % / keine USt"), ("7", "7 %"), ("19", "19 %")]


class IncomeForm(forms.Form):
    received_on = forms.DateField(label="Erhalten am", widget=DATE)
    payer = forms.CharField(label="Von", max_length=200, help_text="z. B. Plattform, Bank, Barkunde")
    category = forms.ChoiceField(label="Art", choices=OtherIncome.Category.choices)
    description = forms.CharField(label="Beschreibung", max_length=300, required=False)
    gross_amount = forms.DecimalField(
        label="Betrag (brutto, €)", min_value=Decimal("0.01"), max_digits=12, decimal_places=2, localize=True
    )
    vat_rate = forms.ChoiceField(label="Enthaltene Umsatzsteuer", choices=RATES, initial="0")
    method = forms.ChoiceField(label="Eingang auf", choices=Expense.Method.choices)
    note = forms.CharField(label="Notiz", max_length=500, required=False)
    receipt = forms.FileField(
        label="Beleg (z. B. Abrechnung der Plattform)",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*,application/pdf,.xml"}),
    )


class CancelForm(forms.Form):
    reason = forms.CharField(label="Grund für den Storno", max_length=300)


@company_view
def income_list(request):
    year = _year(request)
    rows = OtherIncome.objects.filter(company=request.company, received_on__year=year)
    total = rows.filter(cancelled_at__isnull=True).aggregate(total=Sum("gross_amount"))["total"] or Decimal("0")
    context = {"rows": rows, "year": year, "years": _years(), "total": total}
    return render(request, "web/income/list.html", context)


@company_view
def income_create(request):
    form = IncomeForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        upload = data.pop("receipt")
        data["vat_rate"] = Decimal(data["vat_rate"])
        try:
            income = other_income.create_income(
                request.company,
                user=request.user,
                receipt=upload.read() if upload else None,
                receipt_filename=upload.name if upload else "",
                **data,
            )
        except other_income.IncomeError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Einnahme {income.number} erfasst.")
            return redirect(f"/einnahmen/{income.pk}/")
    return render(request, "web/income/form.html", {"form": form})


@company_view
def income_detail(request, pk: int):
    income = get_object_or_404(OtherIncome, pk=pk, company=request.company)
    regime = other_income.regime_on(request.company, income.received_on)
    context = {
        "income": income,
        "tax": other_income.split(income, regime),
        "turnover": other_income.counts_as_turnover(income),
        "cancel_form": CancelForm(),
    }
    return render(request, "web/income/detail.html", context)


@require_POST
@company_view
@full_access
def income_cancel(request, pk: int):
    income = get_object_or_404(OtherIncome, pk=pk, company=request.company)
    form = CancelForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Bitte einen Grund angeben.")
        return redirect(f"/einnahmen/{income.pk}/")
    try:
        other_income.cancel_income(income, reason=form.cleaned_data["reason"], user=request.user)
    except other_income.IncomeError as exc:
        messages.error(request, str(exc))
    return redirect(f"/einnahmen/{income.pk}/")


@company_view
def income_receipt(request, pk: int):
    income = get_object_or_404(OtherIncome, pk=pk, company=request.company)
    if not income.receipt_id:
        raise Http404
    inline = income.receipt.mime in {"application/pdf", "image/png", "image/jpeg", "image/webp"}
    response = FileResponse(
        io.BytesIO(load(income.receipt)),
        as_attachment=not inline,
        filename=income.receipt_filename or income.number,
        content_type=income.receipt.mime,
    )
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"  # uploaded content
    return response
