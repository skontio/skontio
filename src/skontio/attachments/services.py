"""Dokumentenablage: upload with type detection by content, archive storage, deletion only when allowed."""

from __future__ import annotations

import re
import unicodedata
import zipfile
from io import BytesIO

from django.db import transaction
from django.utils import timezone

from skontio.archive.services import release, retention_end, store
from skontio.attachments.models import Attachment
from skontio.audit.services import record

MAX_BYTES = 20 * 1024 * 1024
LETTER_RETENTION_YEARS = 6  # Handels- und Geschäftsbriefe, § 147 Abs. 3 AO
OFFICE = {
    ".docx": ("word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xlsx": ("xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".odt": ("content.xml", "application/vnd.oasis.opendocument.text"),
    ".ods": ("content.xml", "application/vnd.oasis.opendocument.spreadsheet"),
}


class AttachmentError(ValueError):
    pass


def detect_mime(data: bytes, filename: str) -> str:
    """Allowlist by magic bytes – the browser's Content-Type and the extension alone are not trusted."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"PK\x03\x04"):
        extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension in OFFICE:
            marker, mime = OFFICE[extension]
            try:
                names = zipfile.ZipFile(BytesIO(data)).namelist()
            except zipfile.BadZipFile:
                names = []
            if any(name.startswith(marker) for name in names):
                return mime
        raise AttachmentError("ZIP-Archive sind nicht erlaubt (nur Word/Excel/LibreOffice-Dokumente)")
    if b"\x00" not in data[:8192]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            return "application/xml" if text.lstrip().startswith("<?xml") else "text/plain"
    raise AttachmentError("Dateityp nicht erlaubt (PDF, Bilder, Text/XML, Word, Excel, LibreOffice)")


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f\"<>|:*?]", "_", name).strip(" .") or "datei"
    return name[:200]


def add_attachment(
    company, data: bytes, *, filename: str, customer=None, document=None, note="", keep=False, user=None
) -> Attachment:
    if not data:
        raise AttachmentError("Die Datei ist leer")
    if len(data) > MAX_BYTES:
        raise AttachmentError("Die Datei ist größer als 20 MB")
    if customer is None and document is None:
        raise AttachmentError("Datei braucht einen Kunden oder ein Dokument")
    for owner in (customer, document):
        if owner is not None and owner.company_id != company.pk:
            raise AttachmentError("Kunde/Dokument gehört zu einer anderen Firma")
    if document is not None and customer is None:
        customer = document.customer
    mime = detect_mime(data, filename)
    retention = retention_end(timezone.localdate(), LETTER_RETENTION_YEARS) if keep else None
    with transaction.atomic():
        attachment = Attachment.objects.create(
            company=company,
            customer=customer,
            document=document,
            blob=store(data, mime=mime, retention_until=retention),
            filename=safe_filename(filename),
            mime=mime,
            note=note.strip()[:500],
            uploaded_by=user,
        )
        record(
            "attachment.add",
            actor=user,
            company=company,
            entity="attachment",
            entity_id=attachment.pk,
            data={"sha256": attachment.blob.sha256, "keep": keep},
        )
    return attachment


def delete_attachment(attachment: Attachment, *, user=None) -> bool:
    """Remove the entry; the file itself is only deleted when no retention applies. Returns file deleted?"""
    blob = attachment.blob
    with transaction.atomic():
        record(
            "attachment.delete",
            actor=user,
            company=attachment.company,
            entity="attachment",
            entity_id=attachment.pk,
            data={"sha256": blob.sha256},
        )
        attachment.delete()
        return release(blob)


# --- Standardanhänge (AGB, Datenschutzerklärung, Widerrufsbelehrung …) --------------------------------

STANDARD_KINDS = {"quote": "Angebote", "order_confirmation": "Auftragsbestätigungen", "invoice": "Rechnungen"}
MAX_STANDARD_FILES = 10


def standard_kind(doc_type: str) -> str:
    """Abschlags- und Schlussrechnungen get the invoice files."""
    return "invoice" if doc_type in ("invoice", "partial_invoice", "final_invoice") else doc_type


def add_standard_file(company, data: bytes, *, filename: str, send_with: list[str], user=None) -> Attachment:
    """A company-wide file sent along with the chosen document types. PDF only: it goes to customers."""
    kinds = sorted(set(send_with))
    if not kinds or not set(kinds) <= set(STANDARD_KINDS):
        raise AttachmentError("Bitte wählen, bei welchen Dokumenten die Datei mitgeschickt wird.")
    if not data:
        raise AttachmentError("Die Datei ist leer")
    if len(data) > MAX_BYTES:
        raise AttachmentError("Die Datei ist größer als 20 MB")
    if detect_mime(data, filename) != "application/pdf":
        raise AttachmentError("Standardanhänge bitte als PDF hochladen.")
    if standard_files(company).count() >= MAX_STANDARD_FILES:
        raise AttachmentError(f"Höchstens {MAX_STANDARD_FILES} Standardanhänge.")
    with transaction.atomic():
        attachment = Attachment.objects.create(
            company=company,
            blob=store(
                data,
                mime="application/pdf",
                retention_until=retention_end(timezone.localdate(), LETTER_RETENTION_YEARS),
            ),
            filename=safe_filename(filename),
            mime="application/pdf",
            send_with=kinds,
            uploaded_by=user,
        )
        record("attachment.standard_add", actor=user, company=company, entity="attachment", entity_id=attachment.pk,
               data={"sha256": attachment.blob.sha256, "send_with": kinds})  # fmt: skip
    return attachment


def standard_files(company, doc_type: str | None = None):
    files = Attachment.objects.filter(company=company, customer__isnull=True, document__isnull=True)
    if doc_type is not None:
        kind = standard_kind(doc_type)
        files = [f for f in files.select_related("blob") if kind in (f.send_with or [])]
    return files
