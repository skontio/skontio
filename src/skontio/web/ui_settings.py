"""Settings → Oberfläche: choose the look of the web interface (Standard or Modern) for the company."""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render

from skontio.audit.services import record
from skontio.core.models import UiTheme
from skontio.web.support import admin_required, current_company


@admin_required
def ui_theme(request):
    company = current_company()
    if company is None:
        return redirect("setup")
    if request.method == "POST":
        chosen = request.POST.get("ui_theme", "")
        if chosen not in UiTheme.values:
            messages.error(request, "Unbekannte Oberfläche.")
        else:
            if chosen != company.ui_theme:
                before, company.ui_theme = company.ui_theme, chosen
                company.save(update_fields=["ui_theme"])
                record(
                    "company.ui_theme_changed", actor=request.user, company=company, entity="company",
                    entity_id=company.pk, data={"from": before, "to": chosen},
                )  # fmt: skip
            messages.success(request, "Oberfläche gespeichert.")
            return redirect("ui_theme")
    return render(request, "web/ui_theme.html", {"company": company, "themes": UiTheme.choices})
