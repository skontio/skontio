"""Send finalized documents via the configured SMTP server and keep a Versandprotokoll."""

from __future__ import annotations

import hashlib
import smtplib
from email.utils import make_msgid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email

from skontio.archive.services import load
from skontio.documents.models import Document, Rendition
from skontio.documents.renditions import TITLES, de_amount
from skontio.mail.models import MailLog

ATTACHMENT_ORDER = [
    (Rendition.Kind.ZUGFERD_PDF, ".pdf"),
    (Rendition.Kind.PDF, ".pdf"),
    (Rendition.Kind.XRECHNUNG_XML, "-xrechnung.xml"),
]


class MailError(ValueError):
    pass


def default_message(document: Document) -> tuple[str, str]:
    title = TITLES[Document.Type(document.doc_type)]
    company = document.company.name
    subject = f"{title} {document.number} von {company}"
    lines = ["Guten Tag,", "", f"anbei erhalten Sie unsere {title} {document.number}."]
    if document.doc_type == Document.Type.INVOICE and document.due_date:
        lines.append(
            f"Bitte überweisen Sie {de_amount(document.gross_total)} € bis zum {document.due_date:%d.%m.%Y} "
            f"unter Angabe der Rechnungsnummer."
        )
        lines.append("Die angehängte PDF-Datei enthält die E-Rechnung im Format ZUGFeRD (EN 16931).")
    lines += ["", "Mit freundlichen Grüßen", document.company.contact_name or company]
    return subject, "\n".join(lines)


def _clean_addresses(addresses: list[str]) -> list[str]:
    cleaned = []
    for address in addresses:
        address = address.strip()
        if not address:
            continue
        try:
            validate_email(address)
        except ValidationError as exc:
            raise MailError(f"Ungültige E-Mail-Adresse: {address}") from exc
        cleaned.append(address)
    return cleaned


def send_document(
    document: Document,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    sent_by=None,
) -> MailLog:
    if document.status != Document.Status.FINALIZED:
        raise MailError("Nur festgeschriebene Dokumente können versendet werden")
    recipients, copies = _clean_addresses(to), _clean_addresses(cc or [])
    if not recipients:
        raise MailError("Mindestens ein Empfänger ist nötig")
    if "\n" in subject or "\r" in subject:
        raise MailError("Der Betreff darf keine Zeilenumbrüche enthalten")

    renditions = {r.kind: r for r in document.renditions.select_related("blob")}
    attachments = []
    for kind, suffix in ATTACHMENT_ORDER:
        rendition = renditions.get(kind)
        if rendition is None or (kind == Rendition.Kind.PDF and Rendition.Kind.ZUGFERD_PDF in renditions):
            continue
        attachments.append((f"{document.number}{suffix}", load(rendition.blob), rendition.blob.mime))

    sender_domain = settings.DEFAULT_FROM_EMAIL.rsplit("@", 1)[-1].strip(">") or None
    message_id = make_msgid(domain=sender_domain)
    message = EmailMessage(
        subject=subject.strip(),
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
        cc=copies,
        reply_to=[document.company.email] if document.company.email else None,
        headers={"Message-ID": message_id},
    )
    for name, content, mime in attachments:
        message.attach(name, content, mime)

    log = MailLog(
        document=document,
        to=recipients,
        cc=copies,
        subject=subject.strip(),
        message_id=message_id,
        attachments=[{"name": name, "sha256": hashlib.sha256(content).hexdigest()} for name, content, _ in attachments],
        sent_by=sent_by,
    )
    try:
        message.send(fail_silently=False)
    except (OSError, smtplib.SMTPException) as exc:
        log.status, log.error = MailLog.Status.FAILED, str(exc)[:2000]
        log.save()
        raise MailError(f"Versand fehlgeschlagen: {exc}") from exc
    log.status = MailLog.Status.SENT
    log.save()
    return log
