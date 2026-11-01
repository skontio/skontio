"""Einstellungen → Buchhaltung (Kontenrahmen, DATEV-Kennungen, Konten) und Auswertungen → DATEV-Export."""

from __future__ import annotations

import datetime as dt

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from skontio.accounting.accounts import (
    EXPENSES,
    INCOME,
    MONEY,
    AccountError,
    ledger_settings,
    suggested,
    validate_account,
)
from skontio.accounting.datev import DatevError, bookings, export, filename
from skontio.accounting.models import AccountAssignment, LedgerSettings
from skontio.audit.services import record
from skontio.web.support import admin_required, company_view, current_company, full_access

GROUPS = [("Geldkonten", MONEY), ("Erlöse", INCOME), ("Ausgaben", EXPENSES)]


def _int_or_none(value: str, low: int, high: int, label: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if not value.isascii() or not value.isdigit() or not low <= int(value) <= high:
        raise AccountError(f"{label}: ganze Zahl zwischen {low} und {high}.")
    return int(value)


@admin_required
def ledger(request):
    company = current_company()
    if company is None:
        return redirect("setup")
    settings = ledger_settings(company)
    own = dict(AccountAssignment.objects.filter(company=company).values_list("key", "account"))
    error = ""
    if request.method == "POST":
        try:
            chart = request.POST.get("chart", "")
            if chart not in LedgerSettings.Chart.values:
                raise AccountError("Unbekannter Kontenrahmen.")
            length = _int_or_none(request.POST.get("account_length", ""), 4, 8, "Sachkontenlänge") or 4
            consultant = _int_or_none(request.POST.get("consultant_number", ""), 1001, 9999999, "Beraternummer")
            client = _int_or_none(request.POST.get("client_number", ""), 1, 99999, "Mandantennummer")
            wanted = {}
            for _title, purposes in GROUPS:
                for purpose in purposes:
                    value = request.POST.get(f"account-{purpose.key}", "").strip()
                    effective = validate_account(value or suggested(chart, purpose.key), length)
                    if value:
                        wanted[purpose.key] = effective
        except AccountError as exc:
            error = str(exc)
        else:
            with transaction.atomic():
                settings.chart, settings.account_length = chart, length
                settings.consultant_number, settings.client_number = consultant, client
                settings.save()
                AccountAssignment.objects.filter(company=company).exclude(key__in=wanted).delete()
                for key, account in wanted.items():
                    AccountAssignment.objects.update_or_create(company=company, key=key, defaults={"account": account})
                record(
                    "accounting.settings_changed", actor=request.user, company=company, entity="ledger_settings",
                    entity_id=settings.pk, data={"chart": chart, "length": length, "accounts": wanted},
                )  # fmt: skip
            messages.success(request, "Buchhaltung gespeichert.")
            return redirect("ledger")
    groups = [
        (title, [{"p": p, "suggested": suggested(settings.chart, p.key), "own": own.get(p.key, "")} for p in purposes])
        for title, purposes in GROUPS
    ]
    return render(
        request, "web/accounting/ledger.html",
        {"settings": settings, "charts": LedgerSettings.Chart.choices, "groups": groups, "error": error},
    )  # fmt: skip


def _date(value: str, fallback: dt.date) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


@company_view
@full_access
def datev_export(request):
    today = timezone.localdate()
    start = _date(request.GET.get("von", ""), dt.date(today.year, 1, 1))
    end = _date(request.GET.get("bis", ""), today)
    error, rows = "", []
    try:
        if request.GET.get("download"):
            data = export(request.company, start, end)
            record(
                "accounting.datev_export", actor=request.user, company=request.company, entity="company",
                entity_id=request.company.pk, data={"from": start.isoformat(), "to": end.isoformat()},
            )  # fmt: skip
            response = HttpResponse(data, content_type="text/csv; charset=windows-1252")
            response["Content-Disposition"] = f'attachment; filename="{filename(start, end)}"'
            return response
        rows = bookings(request.company, start, end)
    except DatevError as exc:
        error = str(exc)
    return render(
        request, "web/accounting/datev.html",
        {"start": start, "end": end, "rows": rows, "error": error, "review": sum(1 for r in rows if r.review),
         "settings": ledger_settings(request.company)},
    )  # fmt: skip
