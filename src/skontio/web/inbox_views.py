"""E-Rechnungs-Eingang: upload, human-readable view, original download."""

from __future__ import annotations

import io
from decimal import Decimal

from django import forms
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render

from skontio.archive.services import load
from skontio.documents.renditions import de_amount, de_quantity
from skontio.inbox.models import IncomingInvoice
from skontio.inbox.services import MAX_UPLOAD_BYTES, InboxError, receive
from skontio.web.support import company_view


class UploadForm(forms.Form):
    file = forms.FileField(label="E-Rechnung (ZUGFeRD-PDF oder XRechnung-XML)")


def _amount(value) -> str:
    return de_amount(Decimal(str(value))) if value not in (None, "") else ""


@company_view
def inbox_list(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        if upload.size > MAX_UPLOAD_BYTES:
            form.add_error("file", "Die Datei ist größer als 20 MB.")
        else:
            try:
                incoming = receive(request.company, upload.read(), filename=upload.name, received_by=request.user)
                return redirect(f"/eingang/{incoming.pk}/")
            except InboxError as exc:
                form.add_error("file", str(exc))
    invoices = IncomingInvoice.objects.filter(company=request.company)[:200]
    rows = [(inv, _amount(inv.gross_total)) for inv in invoices]
    return render(request, "web/inbox/list.html", {"form": form, "rows": rows})


@company_view
def inbox_detail(request, pk: int):
    incoming = get_object_or_404(IncomingInvoice, pk=pk, company=request.company)
    lines = [
        {
            "name": line["name"],
            "quantity": de_quantity(Decimal(line["quantity"])) if line["quantity"] else "",
            "unit": line["unit_code"],
            "net": _amount(line["net_amount"]),
        }
        for line in incoming.data.get("lines", [])
    ]
    totals = {key: _amount(getattr(incoming, key)) for key in ("net_total", "tax_total", "gross_total")}
    return render(
        request,
        "web/inbox/detail.html",
        {"incoming": incoming, "lines": lines, "totals": totals, "warnings": incoming.data.get("warnings", [])},
    )


@company_view
def inbox_original(request, pk: int):
    incoming = get_object_or_404(IncomingInvoice, pk=pk, company=request.company)
    name = incoming.filename or f"eingang-{incoming.pk}"
    return FileResponse(
        io.BytesIO(load(incoming.original)), as_attachment=True, filename=name, content_type=incoming.original.mime
    )
