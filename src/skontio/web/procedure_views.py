"""Einstellungen → Verfahrensdokumentation: generate a new version, list and download versions."""

from __future__ import annotations

import io

from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from skontio.archive.services import load
from skontio.core.models import ProcedureDocument
from skontio.core.procedure import ProcedureError, generate
from skontio.web.support import admin_required, current_company


@admin_required
def procedure(request):
    company = current_company()
    if company is None:
        return redirect("setup")
    versions = ProcedureDocument.objects.filter(company=company).select_related("created_by", "blob")
    return render(request, "web/procedure.html", {"versions": versions})


@admin_required
@require_POST
def procedure_generate(request):
    try:
        document = generate(current_company(), note=request.POST.get("note", ""), user=request.user)
    except ProcedureError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Version {document.version} erstellt und archiviert.")
    return redirect("procedure")


@admin_required
def procedure_download(request, pk: int):
    document = get_object_or_404(ProcedureDocument, pk=pk, company=current_company())
    filename = f"verfahrensdokumentation-v{document.version}.pdf"
    return FileResponse(io.BytesIO(load(document.blob)), as_attachment=True, filename=filename)
