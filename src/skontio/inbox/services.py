from __future__ import annotations

import hashlib

from django.db import transaction
from django.utils import timezone

from skontio.archive.services import retention_end, store
from skontio.einvoice.reader import EInvoiceParseError, parse_einvoice
from skontio.inbox.models import IncomingInvoice

RETENTION_YEARS = 8  # Buchungsbelege, § 147 Abs. 3 AO
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class InboxError(ValueError):
    pass


def receive(company, data: bytes, *, filename: str = "", received_by=None) -> IncomingInvoice:
    """Parse first, archive only valid e-invoices; the same file twice yields the existing record."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise InboxError("Die Datei ist größer als 20 MB")
    digest = hashlib.sha256(data).hexdigest()
    existing = IncomingInvoice.objects.filter(company=company, original__sha256=digest).first()
    if existing:
        return existing
    try:
        parsed = parse_einvoice(data)
    except EInvoiceParseError as exc:
        raise InboxError(str(exc)) from exc

    retention = retention_end(parsed.issue_date or timezone.localdate(), RETENTION_YEARS)
    mime = "application/pdf" if parsed.source == "pdf" else "application/xml"
    with transaction.atomic():
        original = store(data, mime=mime, retention_until=retention)
        xml_blob = original
        if parsed.source == "pdf":
            xml_blob = store(parsed.xml, mime="application/xml", retention_until=retention)
        return IncomingInvoice.objects.create(
            company=company,
            original=original,
            xml=xml_blob,
            filename=filename[:255],
            syntax=parsed.syntax,
            number=parsed.number[:100],
            seller_name=parsed.seller_name[:300],
            seller_vat_id=parsed.seller_vat_id[:30],
            issue_date=parsed.issue_date,
            due_date=parsed.due_date,
            currency=parsed.currency[:3],
            net_total=parsed.net_total,
            tax_total=parsed.tax_total,
            gross_total=parsed.gross_total,
            data=parsed.to_json(),
            received_by=received_by,
        )
