"""Wiederkehrende Rechnungen: one invoice *draft* per period with the period as Leistungszeitraum.

Periods are counted from ``start_date`` (no drift through short months). Billing in advance creates
the draft on the first day of the period, otherwise on the day after it ends. Missed periods are
caught up one by one. ``RecurringRun`` is unique per period, so a double click or two cron runs at
the same time never create the same period twice. Finalizing and sending stay with a person unless the
recurring invoice has ``auto_send``: then the scheduled run finalizes and mails each new draft (see ``deliver``).
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from skontio.attachments.services import standard_files
from skontio.audit.services import record
from skontio.core.alerts import notify_admins
from skontio.documents.models import INVOICE_TYPES, Document
from skontio.documents.services import DocumentError, add_line, create_draft, finalize
from skontio.mail.models import MailLog
from skontio.mail.services import MailError, default_message, send_document
from skontio.recurring.models import RecurringInvoice, RecurringLine, RecurringRun
from skontio.tax.engine import TaxTreatment

log = logging.getLogger(__name__)
RETRY_DAYS = 7

MAX_CATCH_UP = 24  # never create more than two years of drafts in one go


class RecurringError(ValueError):
    pass


def add_months(day: dt.date, months: int) -> dt.date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def period(recurring: RecurringInvoice, index: int) -> tuple[dt.date, dt.date]:
    start = add_months(recurring.start_date, index * recurring.interval)
    end = add_months(recurring.start_date, (index + 1) * recurring.interval) - dt.timedelta(days=1)
    return start, end


def issue_date_for(recurring: RecurringInvoice, index: int) -> dt.date:
    start, end = period(recurring, index)
    if recurring.end_date and end > recurring.end_date:
        end = recurring.end_date
    return start if recurring.in_advance else end + dt.timedelta(days=1)


def next_issue_date(recurring: RecurringInvoice) -> dt.date | None:
    """When the next draft is due, or None when the contract is paused or has ended."""
    start, _ = period(recurring, recurring.generated)
    if not recurring.active or (recurring.end_date and start > recurring.end_date):
        return None
    return issue_date_for(recurring, recurring.generated)


def due(company, today: dt.date) -> list[RecurringInvoice]:
    found = []
    for recurring in RecurringInvoice.objects.filter(company=company, active=True).select_related("customer"):
        when = next_issue_date(recurring)
        if when is not None and when <= today:
            found.append(recurring)
    return found


def create_from_invoice(
    invoice: Document,
    *,
    name: str,
    interval: int,
    start_date: dt.date,
    in_advance: bool = True,
    auto_send: bool = False,
    end_date: dt.date | None = None,
    user=None,
) -> RecurringInvoice:
    """Turn an existing invoice (draft or finalized) into a recurring one: customer, texts and lines."""
    if invoice.doc_type not in INVOICE_TYPES or invoice.customer is None:
        raise RecurringError("Nur Rechnungen mit Kunde lassen sich als wiederkehrend anlegen.")
    if not invoice.lines.exists():
        raise RecurringError("Die Rechnung hat keine Positionen.")
    if interval not in RecurringInvoice.Interval.values:
        raise RecurringError("Unbekannter Rhythmus.")
    if end_date and end_date < start_date:
        raise RecurringError("Das Ende liegt vor dem Beginn.")
    if not name.strip():
        raise RecurringError("Bitte eine Bezeichnung angeben.")
    if auto_send and not invoice.customer.email:
        raise RecurringError("Für den automatischen Versand braucht der Kunde eine E-Mail-Adresse.")
    with transaction.atomic():
        recurring = RecurringInvoice.objects.create(
            company=invoice.company,
            customer=invoice.customer,
            name=name.strip()[:120],
            interval=interval,
            start_date=start_date,
            end_date=end_date,
            in_advance=in_advance,
            auto_send=auto_send,
            language=invoice.language,
            template=invoice.template,
            intro_text=invoice.intro_text,
            closing_text=invoice.closing_text,
            source=invoice,
            created_by=user,
        )
        for line in invoice.lines.all():
            RecurringLine.objects.create(
                recurring=recurring,
                position=line.position,
                item=line.item,
                text=line.text,
                description=line.description,
                quantity=line.quantity,
                unit_code=line.unit_code,
                unit_price=line.unit_price,
                tax_treatment=line.tax_treatment,
                discount_percent=line.discount_percent,
            )
        record(
            "recurring.create",
            actor=user,
            company=invoice.company,
            entity="recurring_invoice",
            entity_id=recurring.pk,
            data={"from": invoice.pk, "interval": interval, "start": start_date.isoformat()},
        )
    return recurring


def _generate_one(recurring: RecurringInvoice, today: dt.date, user) -> Document | None:
    with transaction.atomic():
        recurring = RecurringInvoice.objects.select_for_update().get(pk=recurring.pk)
        when = next_issue_date(recurring)
        if when is None or when > today:
            return None
        start, end = period(recurring, recurring.generated)
        if recurring.end_date and end > recurring.end_date:
            end = recurring.end_date  # last, shortened period
        draft = create_draft(
            recurring.company,
            Document.Type.INVOICE,
            recurring.customer,
            issue_date=today,
            service_period=(start, end),
            origin=Document.Origin.RECURRING,
            created_by=user,
            language=recurring.language,
            intro_text=recurring.intro_text,
            closing_text=recurring.closing_text,
            template=recurring.template,
        )
        for line in recurring.lines.all():
            add_line(
                draft,
                text=line.text,
                description=line.description,
                quantity=line.quantity,
                unit_code=line.unit_code,
                unit_price=line.unit_price,
                tax_treatment=TaxTreatment(line.tax_treatment),
                discount_percent=Decimal(line.discount_percent),
                item=line.item,
            )
        try:
            with transaction.atomic():
                RecurringRun.objects.create(recurring=recurring, period_start=start, period_end=end, document=draft)
        except IntegrityError:
            transaction.set_rollback(True)  # this period exists already – drop our draft
            return None
        recurring.generated += 1
        recurring.save(update_fields=["generated"])
        record(
            "recurring.generate",
            actor=user,
            company=recurring.company,
            entity="document",
            entity_id=draft.pk,
            data={"recurring": recurring.pk, "period": [start.isoformat(), end.isoformat()]},
        )
    return draft


def generate(recurring: RecurringInvoice, *, today: dt.date, user=None) -> list[Document]:
    """Create all drafts that are due up to ``today`` (catching up missed periods)."""
    if not recurring.lines.exists():
        raise RecurringError(f"„{recurring.name}“ hat keine Positionen.")
    drafts = []
    for _ in range(MAX_CATCH_UP):
        draft = _generate_one(recurring, today, user)
        if draft is None:
            break
        drafts.append(draft)
    return drafts


def _mark_delivery(draft: Document, failed_at) -> None:
    RecurringRun.objects.filter(document=draft).update(send_failed_at=failed_at)


def deliver(recurring: RecurringInvoice, draft: Document, *, notify: bool = True) -> bool:
    """Finalize a generated draft (if it still is one) and mail it to the customer. On any problem the document
    stays as far along as it got, the run is marked (``send_failed_at``, retried daily for a week), the failure
    goes to the audit trail and, with ``notify``, to the admins."""
    try:
        if not recurring.customer.email:
            raise MailError("Der Kunde hat keine E-Mail-Adresse")
        invoice = finalize(draft) if draft.status == Document.Status.DRAFT else draft
        subject, body = default_message(invoice)
        send_document(
            invoice,
            to=[recurring.customer.email],
            subject=subject,
            body=body,
            extra=standard_files(invoice.company, invoice.doc_type),
        )
    except (DocumentError, MailError) as exc:
        log.warning("Automatischer Versand von „%s“ fehlgeschlagen: %s", recurring.name, exc)
        _mark_delivery(draft, timezone.now())
        record(
            "recurring.auto_send_failed",
            company=recurring.company,
            entity="document",
            entity_id=draft.pk,
            data={"recurring": recurring.pk, "error": str(exc)[:300]},
        )
        if notify:
            notify_admins(
                f"Automatischer Rechnungsversand fehlgeschlagen ({recurring.customer.name})",
                [
                    f"Die wiederkehrende Rechnung „{recurring.name}“ für {recurring.customer.name} wurde angelegt,"
                    " konnte aber nicht automatisch versendet werden:",
                    "",
                    str(exc),
                    "",
                    f"Die Rechnung liegt hier bereit: {settings.PUBLIC_URL}/dokumente/{draft.pk}/",
                    f"Skontio versucht es {RETRY_DAYS} Tage lang täglich erneut; du kannst sie auch selbst versenden.",
                ],
            )
        return False
    _mark_delivery(draft, None)
    record(
        "recurring.auto_send",
        company=recurring.company,
        entity="document",
        entity_id=draft.pk,
        data={"recurring": recurring.pk},
    )
    return True


def retry_failed(*, now=None) -> int:
    """Try again for invoices whose automatic delivery failed, for up to ``RETRY_DAYS``. Never mails twice:
    a document that was sent meanwhile (by hand or by an earlier try) just loses its marker."""
    now = now or timezone.now()
    runs = RecurringRun.objects.filter(
        send_failed_at__gte=now - dt.timedelta(days=RETRY_DAYS), recurring__auto_send=True
    ).select_related("recurring__customer", "recurring__company", "document")
    tried = 0
    for run in runs:
        if MailLog.objects.filter(document=run.document, status=MailLog.Status.SENT).exists():
            _mark_delivery(run.document, None)
            continue
        tried += 1
        deliver(run.recurring, run.document, notify=False)
    return tried


def generate_due(company, *, today: dt.date, user=None, auto_send: bool = False) -> list[Document]:
    """Create due drafts. ``auto_send`` (scheduler and CLI only) also finalizes and mails those of recurring
    invoices that have it switched on and tells the admins about the others, which wait for a person to review
    and finalize them; the web buttons leave every draft for a person to review."""
    drafts = []
    waiting: list[tuple[RecurringInvoice, Document]] = []
    for recurring in due(company, today):
        new = generate(recurring, today=today, user=user)
        if auto_send and recurring.auto_send:
            for draft in new:
                deliver(recurring, draft)
        elif auto_send:
            waiting += [(recurring, draft) for draft in new]
        drafts += new
    if waiting:
        notify_waiting(waiting)
    return drafts


def notify_waiting(waiting: list[tuple[RecurringInvoice, Document]]) -> bool:
    """One mail to the admins: these generated invoices are drafts and need to be finalized by a person."""
    lines = ["Wiederkehrende Rechnungen warten darauf, geprüft und festgeschrieben zu werden:", ""]
    lines += [
        f"- {recurring.name} ({recurring.customer.name}): {settings.PUBLIC_URL}/dokumente/{draft.pk}/"
        for recurring, draft in waiting
    ]
    subject = (
        "Rechnung zum Festschreiben bereit"
        if len(waiting) == 1
        else f"{len(waiting)} Rechnungen zum Festschreiben bereit"
    )
    return notify_admins(subject, lines)
