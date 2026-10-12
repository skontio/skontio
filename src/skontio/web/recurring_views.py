"""Wiederkehrende Rechnungen: list, create from an invoice, pause/resume, generate due drafts."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from skontio.audit.services import record
from skontio.documents.models import Document
from skontio.recurring.models import RecurringInvoice
from skontio.recurring.services import RecurringError, create_from_invoice, generate, generate_due, next_issue_date
from skontio.web.support import company_view

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class RecurringForm(forms.Form):
    name = forms.CharField(label="Bezeichnung", max_length=120)
    interval = forms.TypedChoiceField(
        label="Rhythmus",
        choices=RecurringInvoice.Interval.choices,
        coerce=int,
        initial=RecurringInvoice.Interval.MONTHLY,
    )
    start_date = forms.DateField(label="Erster Leistungszeitraum ab", widget=DATE)
    in_advance = forms.BooleanField(
        label="Im Voraus abrechnen (sonst nach Ablauf des Zeitraums)", required=False, initial=True
    )
    end_date = forms.DateField(label="Endet am (optional)", required=False, widget=DATE)


@company_view
def recurring_list(request):
    today = timezone.localdate()
    rows = [
        {"recurring": r, "next": next_issue_date(r), "due": (n := next_issue_date(r)) is not None and n <= today}
        for r in RecurringInvoice.objects.filter(company=request.company).select_related("customer")
    ]
    return render(request, "web/recurring/list.html", {"rows": rows, "due_count": sum(r["due"] for r in rows)})


@require_POST
@company_view
def recurring_from_invoice(request, pk: int):
    invoice = get_object_or_404(Document, pk=pk, company=request.company)
    form = RecurringForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Bitte Bezeichnung, Rhythmus und Beginn angeben.")
        return redirect(f"/dokumente/{invoice.pk}/")
    try:
        recurring = create_from_invoice(invoice, user=request.user, **form.cleaned_data)
    except RecurringError as exc:
        messages.error(request, str(exc))
        return redirect(f"/dokumente/{invoice.pk}/")
    messages.success(request, f"„{recurring.name}“ angelegt. Fällige Rechnungen entstehen als Entwürfe.")
    return redirect("recurring_list")


@require_POST
@company_view
def recurring_generate(request):
    drafts = generate_due(request.company, today=timezone.localdate(), user=request.user)
    if drafts:
        messages.success(request, f"{len(drafts)} Entwurf/Entwürfe angelegt – bitte prüfen und festschreiben.")
    else:
        messages.info(request, "Nichts fällig.")
    return redirect("/dokumente/?typ=invoice")


@require_POST
@company_view
def recurring_toggle(request, pk: int):
    recurring = get_object_or_404(RecurringInvoice, pk=pk, company=request.company)
    recurring.active = not recurring.active
    recurring.save(update_fields=["active"])
    record(
        "recurring.resume" if recurring.active else "recurring.pause",
        actor=request.user,
        company=request.company,
        entity="recurring_invoice",
        entity_id=recurring.pk,
    )
    return redirect("recurring_list")


@require_POST
@company_view
def recurring_run(request, pk: int):
    recurring = get_object_or_404(RecurringInvoice, pk=pk, company=request.company)
    try:
        drafts = generate(recurring, today=timezone.localdate(), user=request.user)
    except RecurringError as exc:
        messages.error(request, str(exc))
        return redirect("recurring_list")
    if not drafts:
        messages.info(request, f"„{recurring.name}“ ist noch nicht fällig.")
        return redirect("recurring_list")
    return redirect(f"/dokumente/{drafts[-1].pk}/")
