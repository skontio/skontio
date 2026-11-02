"""Beleg-Postfach: the worker reads an own IMAP mailbox and files the attachments.

E-invoices (XRechnung, ZUGFeRD) become incoming invoices, other PDFs and photos land in the
Belegeingang. Only unread mails are read; afterwards they are marked as read (and moved, if a
folder is configured). Every mail is recorded by its Message-ID, so it is never imported twice.

Untrusted input: anyone may write to the address. Hence the limits per mail and run, the
optional sender allowlist and the file type check by content (``detect_mime``).
"""

from __future__ import annotations

import hashlib
import imaplib
import logging
import ssl
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from django.db import IntegrityError, transaction

from skontio.attachments.services import MAX_BYTES, AttachmentError, detect_mime
from skontio.core.models import Company
from skontio.expenses.models import ReceiptMail
from skontio.expenses.services import ExpenseError, add_pending
from skontio.inbox.services import InboxError, receive

log = logging.getLogger("skontio.mailbox")

MAX_MAIL_BYTES = 50 * 1024 * 1024
MAX_FILES_PER_MAIL = 10
MAX_MAILS_PER_RUN = 50
MIN_INLINE_IMAGE_BYTES = 50 * 1024  # smaller inline images are logos in signatures, not receipts
TIMEOUT = 60


class MailboxError(RuntimeError):
    pass


@dataclass
class ParsedMail:
    message_id: str
    sender: str
    subject: str
    files: list[tuple[str, bytes]] = field(default_factory=list)
    skipped: int = 0


def _header(message, name: str) -> str:
    try:
        return " ".join(str(message.get(name, "") or "").split())
    except Exception:  # noqa: BLE001 – broken encoded words must not stop the import
        return ""


def parse(raw: bytes) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    message_id = _header(message, "Message-ID")[:255] or "sha256:" + hashlib.sha256(raw).hexdigest()
    sender = parseaddr(_header(message, "From"))[1].lower()[:320]
    mail = ParsedMail(message_id, sender, _header(message, "Subject")[:300])
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        filename = (part.get_filename() or "").strip()
        lower = filename.lower()
        is_pdf = content_type == "application/pdf" or lower.endswith(".pdf")
        is_xml = content_type in ("application/xml", "text/xml") or lower.endswith(".xml")
        is_image = content_type.startswith("image/")
        if not (is_pdf or is_xml or is_image):
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            data = b""
        if is_image and part.get_content_disposition() != "attachment" and len(data) < MIN_INLINE_IMAGE_BYTES:
            continue
        if not data or len(data) > MAX_BYTES or len(mail.files) >= MAX_FILES_PER_MAIL:
            mail.skipped += 1
            continue
        mail.files.append((filename or f"beleg-{len(mail.files) + 1}", data))
    return mail


def sender_allowed(sender: str, allowed: list[str]) -> bool:
    """Empty list: everyone. Entries are addresses (``a@b.de``) or domains (``b.de``)."""
    if not allowed:
        return True
    domain = sender.rpartition("@")[2]
    return any(sender == entry if "@" in entry else domain == entry for entry in allowed)


def import_mail(company, raw: bytes, *, allowed_senders: list[str] = ()) -> ReceiptMail | None:
    """File one mail; None if this Message-ID was imported before."""
    mail = parse(raw)
    if ReceiptMail.objects.filter(company=company, message_id=mail.message_id).exists():
        return None
    invoices = receipts = 0
    skipped = mail.skipped
    allowed = sender_allowed(mail.sender, list(allowed_senders))
    note = f"Per E-Mail von {mail.sender}: {mail.subject}"[:500]
    try:
        with transaction.atomic():  # receipts and the record of the mail together, or nothing
            for filename, data in mail.files if allowed else ():
                try:
                    mime = detect_mime(data, filename)
                except AttachmentError:
                    skipped += 1
                    continue
                if mime in ("application/pdf", "application/xml"):
                    try:
                        with transaction.atomic():
                            receive(company, data, filename=filename)
                        invoices += 1
                        continue
                    except InboxError:
                        if mime == "application/xml":  # XML that is no e-invoice is no receipt either
                            skipped += 1
                            continue
                try:
                    with transaction.atomic():
                        add_pending(company, data, filename=filename, note=note)
                    receipts += 1
                except ExpenseError:
                    skipped += 1
            if allowed:
                result = f"{receipts} Beleg(e), {invoices} E-Rechnung(en)" + (
                    f", {skipped} übersprungen" if skipped else ""
                )
            else:
                result = "Absender nicht zugelassen – nichts übernommen"
            return ReceiptMail.objects.create(
                company=company, message_id=mail.message_id, sender=mail.sender, subject=mail.subject, result=result
            )
    except IntegrityError:  # imported by a parallel run in the meantime
        return None


def company_for(config: dict) -> Company:
    if config.get("company"):
        company = Company.objects.filter(pk=config["company"]).first()
        if company is None:
            raise MailboxError(f"SKONTIO_RECEIPT_IMAP_COMPANY: Firma {config['company']} gibt es nicht")
        return company
    companies = list(Company.objects.all()[:2])
    if len(companies) != 1:
        raise MailboxError("Mehrere oder keine Firma: bitte SKONTIO_RECEIPT_IMAP_COMPANY setzen")
    return companies[0]


def connect(config: dict) -> imaplib.IMAP4:
    context = ssl.create_default_context()
    if config["security"] == "ssl":
        client = imaplib.IMAP4_SSL(config["host"], config["port"], ssl_context=context, timeout=TIMEOUT)
    else:
        client = imaplib.IMAP4(config["host"], config["port"], timeout=TIMEOUT)
        client.starttls(ssl_context=context)
    client.login(config["user"], config["password"])
    return client


def quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ok(response, what: str):
    status, data = response
    if status != "OK":
        raise MailboxError(f"IMAP {what} fehlgeschlagen: {data!r}"[:300])
    return data


def _size(data) -> int:
    for item in data:
        text = item.decode() if isinstance(item, bytes) else str(item)
        if "RFC822.SIZE" in text:
            return int(text.split("RFC822.SIZE", 1)[1].strip(" ()").split()[0].rstrip(")"))
    return 0


def _raw(data) -> bytes:
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            return item[1]
    return b""


def poll(config: dict, *, client: imaplib.IMAP4 | None = None) -> str:
    """Read up to ``MAX_MAILS_PER_RUN`` unread mails; returns a short summary for the worker log."""
    company = company_for(config)
    client = client or connect(config)
    imported = duplicates = failed = 0
    try:
        _ok(client.select(quote(config["folder"])), "SELECT")
        uids = _ok(client.uid("SEARCH", None, "UNSEEN"), "SEARCH")[0].split()[:MAX_MAILS_PER_RUN]
        can_move = "MOVE" in getattr(client, "capabilities", ())
        for uid in uids:
            flags = "(\\Seen)"
            try:
                if _size(_ok(client.uid("FETCH", uid, "(RFC822.SIZE)"), "FETCH")) > MAX_MAIL_BYTES:
                    raise MailboxError("Mail größer als 50 MB")
                raw = _raw(_ok(client.uid("FETCH", uid, "(BODY.PEEK[])"), "FETCH"))
                if import_mail(company, raw, allowed_senders=config.get("allowed_senders", [])) is None:
                    duplicates += 1
                else:
                    imported += 1
            except Exception:  # noqa: BLE001 – one broken mail must not block the others
                log.exception("Beleg-Mail %s konnte nicht übernommen werden", uid)
                failed += 1
                flags = "(\\Seen \\Flagged)"  # flagged for a look by hand, not retried forever
            client.uid("STORE", uid, "+FLAGS", flags)
            if config.get("done_folder") and flags == "(\\Seen)":
                target = quote(config["done_folder"])
                if can_move:
                    client.uid("MOVE", uid, target)
                elif client.uid("COPY", uid, target)[0] == "OK":
                    client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 – the work is done; a failed goodbye does not matter
            log.debug("IMAP-Abmeldung fehlgeschlagen", exc_info=True)
    summary = f"{imported} Mail(s) übernommen"
    if duplicates:
        summary += f", {duplicates} schon bekannt"
    if failed:
        summary += f", {failed} fehlgeschlagen (im Postfach markiert)"
    return summary
