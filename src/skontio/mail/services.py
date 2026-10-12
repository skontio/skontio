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
from skontio.crm.activities import log_activity
from skontio.crm.models import Activity
from skontio.documents.languages import Lang
from skontio.documents.models import INVOICE_TYPES, Document, Rendition
from skontio.documents.renditions import TITLES
from skontio.documents.services import amount_due
from skontio.mail.models import MailLog

ATTACHMENT_ORDER = [
    (Rendition.Kind.ZUGFERD_PDF, ".pdf"),
    (Rendition.Kind.PDF, ".pdf"),
    (Rendition.Kind.XRECHNUNG_XML, "-xrechnung.xml"),
]


class MailError(ValueError):
    pass


def smtp_configured() -> bool:
    """False when the SMTP backend is selected but no server was entered (SKONTIO_SMTP_HOST)."""
    config = settings.MAILERS.get("default", {})
    if config.get("BACKEND") != settings.SMTP_BACKEND:
        return True
    return bool(config.get("OPTIONS", {}).get("host"))


def default_message(document: Document) -> tuple[str, str]:
    """Suggested subject and text in the document's language (editable before sending)."""
    t = Lang(document.language)
    title = t.title(document.doc_type)
    company = document.company.name
    subject = t("mail_subject", title=title, number=document.number, company=company)
    in_sentence = title if t.code == "de" else title.lower()
    lines = [t("mail_greeting"), "", t("mail_attached", title=in_sentence, number=document.number)]
    if document.doc_type in INVOICE_TYPES and document.due_date:
        lines.append(t("mail_pay", amount=t.money(amount_due(document)), date=t.date(document.due_date)))
        lines.append(t("mail_einvoice"))
    lines += ["", t("mail_regards"), document.company.contact_name or company]
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
    attachments: list[tuple[str, bytes, str]] | None = None,
    activity_text: str = "",
) -> MailLog:
    """Send a finalized document. ``attachments`` (name, content, mime) replace its own files – e.g. a
    reminder letter plus the invoice PDF; ``activity_text`` replaces the customer-history entry."""
    if document.status != Document.Status.FINALIZED:
        raise MailError("Nur festgeschriebene Dokumente können versendet werden")
    if not smtp_configured():
        raise MailError("Kein SMTP-Server eingerichtet (SKONTIO_SMTP_HOST, siehe docs/betrieb.md)")
    recipients, copies = _clean_addresses(to), _clean_addresses(cc or [])
    if not recipients:
        raise MailError("Mindestens ein Empfänger ist nötig")
    if "\n" in subject or "\r" in subject:
        raise MailError("Der Betreff darf keine Zeilenumbrüche enthalten")

    renditions = {r.kind: r for r in document.renditions.select_related("blob")}
    if attachments is None:
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
        message.send()
    except (OSError, smtplib.SMTPException) as exc:
        log.status, log.error = MailLog.Status.FAILED, str(exc)[:2000]
        log.save()
        raise MailError(f"Versand fehlgeschlagen: {exc}") from exc
    log.status = MailLog.Status.SENT
    log.save()
    if document.customer is not None:
        log_activity(
            document.customer,
            kind=Activity.Kind.EMAIL,
            text=activity_text
            or f"{TITLES[Document.Type(document.doc_type)]} {document.number} an {', '.join(recipients)} gesendet",
            document=document,
            user=sent_by,
        )
    return log
