"""Mahnwesen: overview, create and send reminder letters, configure levels."""

from __future__ import annotations

import io
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from skontio.archive.services import load
from skontio.documents.models import Document
from skontio.documents.renditions import de_amount
from skontio.dunning.models import DunningLevel, DunningNotice
from skontio.dunning.services import DunningError, create_notice, levels, overdue, send_notice
from skontio.mail.services import MailError
from skontio.web.support import admin_required, company_view, current_company


def _row(candidate, today):
    return {
        "invoice": candidate.invoice,
        "open": de_amount(candidate.open_amount),
        "days": candidate.days_overdue,
        "last": candidate.last,
        "next": candidate.next_level,
        "due_on": candidate.due_on,
        "due_now": candidate.due_on is not None and candidate.due_on <= today,
    }


@company_view
def dunning_overview(request):
    today = timezone.localdate()
    rows = [_row(c, today) for c in overdue(request.company, today)]
    context = {
        "due": [r for r in rows if r["due_now"]],
        "waiting": [r for r in rows if not r["due_now"]],
        "recent": DunningNotice.objects.filter(company=request.company).select_related("invoice")[:20],
    }
    return render(request, "web/dunning/overview.html", context)


@require_POST
@company_view
def dunning_create(request, pk: int):
    invoice = get_object_or_404(Document, pk=pk, company=request.company)
    try:
        notice = create_notice(invoice, issued_on=timezone.localdate(), user=request.user)
    except DunningError as exc:
        messages.error(request, str(exc))
        return redirect("dunning_overview")
    messages.success(request, f"{notice.title} {notice.number} erstellt – jetzt prüfen und versenden.")
    return redirect(f"/dokumente/{invoice.pk}/#mahnungen")


@company_view
def dunning_pdf(request, pk: int):
    notice = get_object_or_404(DunningNotice, pk=pk, company=request.company)
    return FileResponse(io.BytesIO(load(notice.blob)), filename=f"{notice.number}.pdf", content_type="application/pdf")


@require_POST
@company_view
def dunning_send(request, pk: int):
    notice = get_object_or_404(DunningNotice.objects.select_related("invoice"), pk=pk, company=request.company)
    to = [a for a in request.POST.get("to", "").replace(";", ",").split(",") if a.strip()]
    try:
        send_notice(
            notice,
            to=to,
            subject=request.POST.get("subject", ""),
            body=request.POST.get("body", ""),
            user=request.user,
        )
    except MailError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{notice.title} {notice.number} versendet.")
    return redirect(f"/dokumente/{notice.invoice.pk}/#mahnungen")


class LevelForm(forms.ModelForm):
    class Meta:
        model = DunningLevel
        fields = ["name", "days_after", "pay_days", "fee", "text"]
        widgets = {"text": forms.Textarea(attrs={"rows": 2})}

    def clean_fee(self):
        fee = self.cleaned_data["fee"]
        if fee < Decimal("0"):
            raise forms.ValidationError("Die Gebühr darf nicht negativ sein.")
        return fee


@admin_required
def dunning_levels(request):
    company = current_company()
    if company is None:
        return redirect("setup")
    level_forms = [
        LevelForm(request.POST or None, instance=level, prefix=f"l{level.level}") for level in levels(company)
    ]
    if request.method == "POST" and all(form.is_valid() for form in level_forms):
        for form in level_forms:
            form.save()
        messages.success(request, "Mahnstufen gespeichert.")
        return redirect("dunning_levels")
    return render(request, "web/dunning/levels.html", {"level_forms": level_forms})
