"""Einstellungen → Steuerstatus: history, threshold monitor, confirmed change, Korrektur-Assistent."""

from __future__ import annotations

import datetime as dt

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from skontio.audit.services import record
from skontio.documents.models import Document
from skontio.documents.renditions import de_amount
from skontio.documents.services import DocumentError
from skontio.payments.models import PaymentError
from skontio.tax.engine import Regime
from skontio.taxstatus.corrections import candidates, correct_invoice, transfer_payments
from skontio.taxstatus.models import TaxStatusPeriod
from skontio.taxstatus.services import TaxStatusError, change_regime, regime_for
from skontio.taxstatus.thresholds import small_business_status
from skontio.web.support import admin_required, current_company

Reason = TaxStatusPeriod.Reason
REGIME_LABELS = {Regime.SMALL_BUSINESS: "Kleinunternehmer (§ 19 UStG)", Regime.STANDARD: "Regelbesteuerung"}
REASONS = {
    Regime.STANDARD: [Reason.THRESHOLD_CURRENT_YEAR, Reason.THRESHOLD_PREVIOUS_YEAR, Reason.VOLUNTARY_OPTION],
    Regime.SMALL_BUSINESS: [Reason.RETURN_TO_SMALL_BUSINESS],
}


class ChangeForm(forms.Form):
    valid_from = forms.DateField(
        label="Gilt für Leistungen ab", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    reason = forms.ChoiceField(label="Grund")
    confirm = forms.BooleanField(
        label="Ich bestätige den Wechsel (am besten mit der Steuerberatung abgestimmt).", required=True
    )

    def __init__(self, *args, target: Regime, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].choices = [(r.value, r.label) for r in REASONS[target]]


def _suggestion(status, current: Regime) -> dict:
    if current is not Regime.SMALL_BUSINESS or status is None:
        return {}
    if status.previous_year_exceeded:
        return {"valid_from": dt.date(status.year, 1, 1), "reason": Reason.THRESHOLD_PREVIOUS_YEAR}
    if status.crossed_on:
        return {"valid_from": status.crossed_on, "reason": Reason.THRESHOLD_CURRENT_YEAR}
    return {}


def _state(company):
    today = timezone.localdate()
    current = regime_for(company, today)
    target = Regime.STANDARD if current is Regime.SMALL_BUSINESS else Regime.SMALL_BUSINESS
    status = small_business_status(company, today) if current is Regime.SMALL_BUSINESS else None
    return current, target, status


def _context(company, current, target, status, form) -> dict:
    return {
        "periods": TaxStatusPeriod.objects.filter(company=company).select_related("confirmed_by"),
        "current_label": REGIME_LABELS[current],
        "target_label": REGIME_LABELS[target],
        "status": status,
        "revenue": de_amount(status.revenue) if status else "",
        "limit": de_amount(status.current_limit, 0) if status else "",
        "form": form,
        "candidates": [
            {
                "doc": c.document,
                "recorded": REGIME_LABELS[c.recorded],
                "current": REGIME_LABELS[c.current],
                "gross": de_amount(c.document.gross_total),
                "paid": de_amount(c.paid) if c.paid else "",
                "correctable": c.correctable,
            }
            for c in candidates(company)
        ],
        "regime_labels": {regime.value: label for regime, label in REGIME_LABELS.items()},
    }


@admin_required
def tax_status(request):
    company = current_company()
    if company is None:
        return redirect("setup")
    try:
        current, target, status = _state(company)
    except TaxStatusError:
        return redirect("setup")
    form = ChangeForm(target=target, initial=_suggestion(status, current))
    return render(request, "web/taxstatus.html", _context(company, current, target, status, form))


@admin_required
@require_POST
def tax_status_change(request):
    company = current_company()
    current, target, status = _state(company)
    form = ChangeForm(request.POST, target=target)
    if form.is_valid():
        try:
            period = change_regime(
                company,
                target,
                form.cleaned_data["valid_from"],
                Reason(form.cleaned_data["reason"]),
                confirmed_by=request.user,
            )
        except TaxStatusError as exc:
            form.add_error(None, str(exc))
        else:
            record(
                "taxstatus.change",
                actor=request.user,
                company=company,
                entity="tax_status_period",
                entity_id=period.pk,
                data={"regime": period.regime, "valid_from": period.valid_from.isoformat(), "reason": period.reason},
            )
            messages.success(request, f"Steuerstatus ab {period.valid_from:%d.%m.%Y}: {REGIME_LABELS[target]}.")
            return redirect("tax_status")
    return render(request, "web/taxstatus.html", _context(company, current, target, status, form), status=400)


@admin_required
@require_POST
def invoice_correct(request, pk: int):
    invoice = get_object_or_404(Document, pk=pk, company=current_company())
    keep = request.POST.get("keep", "net")
    try:
        draft = correct_invoice(invoice, keep=keep, issue_date=timezone.localdate(), created_by=request.user)
    except DocumentError as exc:
        messages.error(request, f"Korrektur nicht möglich: {exc}")
        return redirect("tax_status")
    messages.success(
        request,
        f"Rechnung {invoice.number} storniert. Der neue Entwurf ersetzt sie – bitte prüfen und festschreiben.",
    )
    return redirect(f"/dokumente/{draft.pk}/")


@admin_required
@require_POST
def payments_transfer(request, pk: int):
    replacement = get_object_or_404(Document, pk=pk, company=current_company())
    try:
        moved, left = transfer_payments(replacement, created_by=request.user)
    except (DocumentError, PaymentError) as exc:
        messages.error(request, str(exc))
    else:
        text = f"{de_amount(moved)} € von {replacement.replaces.number} übernommen."
        if left:
            text += f" {de_amount(left)} € passen nicht und bleiben bei der alten Rechnung (bitte erstatten)."
        messages.success(request, text)
    return redirect(f"/dokumente/{replacement.pk}/")
