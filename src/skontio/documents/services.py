from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from skontio.documents.models import Document, DocumentLocked
from skontio.numbering.services import allocate_number

__all__ = ["DocumentLocked", "finalize"]


def finalize(document: Document) -> Document:
    """Festschreiben: assign the number and lock the document in one transaction."""
    with transaction.atomic():
        doc = Document.objects.select_for_update().get(pk=document.pk)
        if doc.status != Document.Status.DRAFT:
            raise DocumentLocked(f"Dokument {doc.number} ist bereits festgeschrieben")
        if not doc.lines.exists():
            raise ValueError("Ein Dokument ohne Positionen kann nicht festgeschrieben werden")
        doc.number = allocate_number(doc.company, doc.doc_type, doc.issue_date)
        doc.status = Document.Status.FINALIZED
        doc.locked_at = timezone.now()
        doc.save()
    return doc
