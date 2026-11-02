"""CSV export and import pages for contacts and the catalogue (logic in skontio.web.transfer)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render

from skontio.web import transfer
from skontio.web.csvexport import csv_response
from skontio.web.support import company_view, full_access


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
