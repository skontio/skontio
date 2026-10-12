from __future__ import annotations

from django.contrib import messages
from django.http import FileResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from skontio.archive.services import load
from skontio.attachments.models import Attachment
from skontio.attachments.services import AttachmentError, add_attachment, delete_attachment
from skontio.crm.models import Customer
from skontio.documents.models import Document
from skontio.web.support import company_view


def _owner_url(customer_pk=None, document_pk=None) -> str:
    return f"/dokumente/{document_pk}/#dateien" if document_pk else f"/kunden/{customer_pk}/#dateien"


@require_POST
@company_view
def upload(request):
    customer = document = None
    if pk := request.POST.get("document"):
        document = get_object_or_404(Document, pk=pk, company=request.company)
    elif pk := request.POST.get("customer"):
        customer = get_object_or_404(Customer, pk=pk, company=request.company)
    else:
        return HttpResponseBadRequest("Kunde oder Dokument fehlt")
    upload = request.FILES.get("file")
    if upload is None:
        messages.error(request, "Bitte eine Datei auswählen.")
    else:
        try:
            add_attachment(
                request.company,
                upload.read(),
                filename=upload.name,
                customer=customer,
                document=document,
                note=request.POST.get("note", ""),
                keep=bool(request.POST.get("keep")),
                user=request.user,
            )
            messages.success(request, f"{upload.name} abgelegt.")
        except AttachmentError as exc:
            messages.error(request, str(exc))
    return redirect(_owner_url(customer.pk if customer else None, document.pk if document else None))


@company_view
def download(request, pk: int):
    attachment = get_object_or_404(Attachment, pk=pk, company=request.company)
    inline = attachment.mime in {"application/pdf", "image/png", "image/jpeg", "image/webp"}
    response = FileResponse(
        iter([load(attachment.blob)]),
        content_type=attachment.mime,
        as_attachment=not inline,
        filename=attachment.filename,
    )
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"  # uploaded content is untrusted
    return response


@require_POST
@company_view
def remove(request, pk: int):
    attachment = get_object_or_404(Attachment, pk=pk, company=request.company)
    target = _owner_url(attachment.customer_id, attachment.document_id)
    deleted = delete_attachment(attachment, user=request.user)
    if deleted:
        messages.success(request, "Datei gelöscht.")
    else:
        messages.info(request, "Eintrag entfernt. Die Datei bleibt bis zum Ende der Aufbewahrungsfrist im Archiv.")
    return redirect(target)
