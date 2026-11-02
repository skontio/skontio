"""CSV export and import pages for contacts and the catalogue (logic in skontio.web.transfer)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from skontio.audit.services import record
from skontio.web import full_export as full_export_module
from skontio.web import transfer
from skontio.web.csvexport import csv_response
from skontio.web.support import admin_required, company_view, full_access


class UploadForm(forms.Form):
    file = forms.FileField(label="CSV-Datei", widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}))


KINDS = {
    "kontakte": {
        "title": "Kontakte importieren",
        "back": "/kunden/",
        "columns": transfer.CUSTOMER_COLUMNS,
        "required": ["Name"],
        "run": transfer.import_customers,
        "example": "/kunden/export.csv",
    },
    "leistungen": {
        "title": "Leistungen importieren",
        "back": "/leistungen/",
        "columns": transfer.ITEM_COLUMNS,
        "required": ["Bezeichnung", "Einheit", "Nettopreis"],
        "run": transfer.import_items,
        "example": "/leistungen/export.csv",
    },
}


@company_view
def customers_csv(request):
    return csv_response(
        transfer.export_filename("kontakte"), transfer.CUSTOMER_COLUMNS, transfer.customer_rows(request.company)
    )


@company_view
def items_csv(request):
    return csv_response(
        transfer.export_filename("leistungen"), transfer.ITEM_COLUMNS, transfer.item_rows(request.company)
    )


def _import(request, kind: str):
    spec = KINDS[kind]
    form = UploadForm(request.POST or None, request.FILES or None)
    result = problem = None
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        if upload.size > transfer.MAX_BYTES:
            problem = "Die Datei ist größer als 2 MB."
        else:
            try:
                result = spec["run"](request.company, upload.read(), user=request.user)
            except transfer.FileProblem as exc:
                problem = str(exc)
        if result and not result.errors:
            skipped = f", {len(result.skipped)} übersprungen" if result.skipped else ""
            messages.success(request, f"{result.created} angelegt{skipped}.")
            if not result.skipped:
                return redirect(spec["back"])
    return render(request, "web/import.html", {**spec, "form": form, "result": result, "problem": problem})


@company_view
@full_access
def customers_import(request):
    return _import(request, "kontakte")


@company_view
@full_access
def items_import(request):
    return _import(request, "leistungen")


@admin_required
@company_view
def full_export(request):
    """GET: explanation; POST: the ZIP (POST so a stray link or prefetch never builds it)."""
    if request.method != "POST":
        return render(request, "web/full_export.html")
    out, counts = full_export_module.build_tempfile(request.company)
    record("export.full", actor=request.user, company=request.company, entity="core.Company",
           entity_id=request.company.pk, data=counts)  # fmt: skip
    return FileResponse(
        out,
        as_attachment=True,
        filename=f"skontio-gesamtexport-{timezone.localdate().isoformat()}.zip",
        content_type="application/zip",
    )


@admin_required
@company_view
def papierkram_import(request):
    """Umzug aus Papierkram: first check (nothing saved), then import – same upload, one click each."""
    from skontio.imports.papierkram import PapierkramError, import_papierkram

    report, problem, dry_run = None, "", True
    if request.method == "POST":
        upload = request.FILES.get("export")
        dry_run = request.POST.get("aktion") != "importieren"
        if upload is None:
            problem = "Bitte die ZIP-Datei aus Papierkram auswählen."
        else:
            try:
                report = import_papierkram(
                    request.company, upload.read(), user=request.user, dry_run=dry_run,
                    continue_numbering=request.POST.get("nummern") == "1",
                )  # fmt: skip
            except PapierkramError as exc:
                problem = str(exc)
    lists = {
        "created": sorted(report.created.items()) if report else [],
        "skipped": sorted(report.skipped.items()) if report else [],
        "not_imported": sorted(report.not_imported.items()) if report else [],
    }  # template lookups on a Counter would read the key "items" instead of the method
    return render(request, "web/papierkram_import.html",
                  {"report": report, "problem": problem, "dry_run": dry_run, **lists})  # fmt: skip
