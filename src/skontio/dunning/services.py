"""Mahnwesen: which invoices are due for a reminder, and the reminder letters themselves.

* Levels are configurable per company (defaults: Zahlungserinnerung, 1. Mahnung, 2. Mahnung, no fees).
* A level becomes due ``days_after`` days after the due date (first level) or after the previous
  letter. Skontio only suggests – a person creates and sends every letter.
* Letters use the invoice's snapshot (addresses as on the invoice), its template and language, and
  are archived as PDF/A (Geschäftsbrief, 6 years). Fees are stated in the letter but not booked as a
  receivable; default interest (§ 288 BGB) is not computed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.template.loader import render_to_string

from skontio.archive.services import load, retention_end, store
from skontio.audit.services import record
from skontio.documents.languages import Lang
from skontio.documents.layouts import layout_context, template_for
from skontio.documents.models import INVOICE_TYPES, Document, PartySnapshot
from skontio.documents.renditions import LETTER_RETENTION_YEARS
from skontio.dunning.models import DunningLevel, DunningNotice
from skontio.mail.services import send_document
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import allocate_number
from skontio.payments.services import open_amount
from skontio.rendering.girocode import girocode_svg_data_uri
from skontio.rendering.pdf import render_html_pdf_a3

SEQUENCE, PATTERN = "dunning", "MA-{YYYY}-{####}"
DEFAULT_LEVELS = [
    (1, "Zahlungserinnerung", 7),
    (2, "1. Mahnung", 14),
    (3, "2. Mahnung", 14),
]


class DunningError(ValueError):
    pass


def levels(company) -> list[DunningLevel]:
    existing = list(DunningLevel.objects.filter(company=company).order_by("level"))
    if existing:
        return existing
    with transaction.atomic():
        for level, name, days in DEFAULT_LEVELS:
            DunningLevel.objects.get_or_create(
                company=company, level=level, defaults={"name": name, "days_after": days}
            )
    return list(DunningLevel.objects.filter(company=company).order_by("level"))


@dataclass(frozen=True)
class Candidate:
    invoice: Document
    open_amount: Decimal
    days_overdue: int
    next_level: DunningLevel | None  # None: all levels used
    due_on: dt.date | None  # when the next level becomes due
    last: DunningNotice | None


def _state(invoice: Document, configured: list[DunningLevel], today: dt.date) -> Candidate | None:
    amount = open_amount(invoice)
    if amount <= 0 or invoice.due_date is None or invoice.due_date >= today:
        return None
    notices = sorted(invoice.dunning_notices.all(), key=lambda n: n.level)
    last = notices[-1] if notices else None
    by_level = {level.level: level for level in configured}
    next_level = by_level.get(last.level + 1 if last else 1)
    due_on = None
    if next_level is not None:
        start = last.issued_on if last else invoice.due_date
        due_on = start + dt.timedelta(days=next_level.days_after)
    return Candidate(invoice, amount, (today - invoice.due_date).days, next_level, due_on, last)


def overdue(company, today: dt.date) -> list[Candidate]:
    """All overdue, unpaid, not cancelled invoices with their dunning state (oldest due date first)."""
    configured = levels(company)
    invoices = (
        Document.objects.filter(
            company=company, doc_type__in=INVOICE_TYPES, status=Document.Status.FINALIZED, due_date__lt=today
        )
        .exclude(children__doc_type=Document.Type.CANCELLATION)
        .select_related("customer")
        .prefetch_related("dunning_notices")
        .order_by("due_date", "number")
    )
    return [state for invoice in invoices if (state := _state(invoice, configured, today))]


def candidates(company, today: dt.date) -> list[Candidate]:
    """Overdue invoices whose next reminder level is due today or earlier."""
    return [c for c in overdue(company, today) if c.due_on is not None and c.due_on <= today]


def render_notice_html(notice: DunningNotice) -> str:
    invoice = notice.invoice
    t = Lang(notice.language)
    snapshots = {s.role: s.data for s in invoice.party_snapshots.all()}
    seller, buyer = snapshots[PartySnapshot.Role.SELLER], snapshots[PartySnapshot.Role.BUYER]
    previous = list(DunningNotice.objects.filter(invoice=invoice, level__lt=notice.level).order_by("level"))
    iban = seller.get("iban", "")
    girocode = ""
    if iban and invoice.currency == "EUR":
        girocode = girocode_svg_data_uri(
            name=seller.get("account_holder") or seller["name"],
            iban=iban,
            bic=seller.get("bic", ""),
            amount=notice.total,
            reference=invoice.number,
        )
    custom = DunningLevel.objects.filter(company=notice.company, level=notice.level).values_list("text", flat=True)
    letters = ", ".join(f"{p.title} ({t.date(p.issued_on)})" for p in previous)
    context = {
        **layout_context(template_for(invoice)),
        "t": t.texts,
        "language": t.code,
        "notice": notice,
        "seller": seller,
        "buyer": buyer,
        "iban_formatted": " ".join(iban[i : i + 4] for i in range(0, len(iban), 4)),
        "issued_on": t.date(notice.issued_on),
        "intro": t("dunning_intro_first") if notice.level == 1 else t("dunning_intro_later"),
        "custom_text": custom.first() or "",
        "invoice_line": t(
            "dunning_invoice", number=invoice.number, date=t.date(invoice.issue_date), due=t.date(invoice.due_date)
        ),
        "open_amount": t.money(notice.open_amount),
        "fee": t.money(notice.fee) if notice.fee else "",
        "total": t.money(notice.total),
        "pay_text": t("pay_by", amount=t.money(notice.total), date=t.date(notice.pay_by)),
        "previous": t("dunning_previous", letters=letters) if previous else "",
        "girocode": girocode,
    }
    return render_to_string("documents/dunning.html", context)


def create_notice(invoice: Document, *, issued_on: dt.date, user=None) -> DunningNotice:
    """Create the next reminder letter for ``invoice`` (also before its suggested date, if overdue)."""
    with transaction.atomic():
        invoice = Document.objects.select_for_update().select_related("company").get(pk=invoice.pk)
        if invoice.doc_type not in INVOICE_TYPES or invoice.status != Document.Status.FINALIZED:
            raise DunningError("Gemahnt werden nur festgeschriebene Rechnungen.")
        if invoice.children.filter(doc_type=Document.Type.CANCELLATION).exists():
            raise DunningError(f"Rechnung {invoice.number} ist storniert.")
        state = _state(invoice, levels(invoice.company), issued_on)
        if state is None:
            raise DunningError(f"Rechnung {invoice.number} ist nicht überfällig oder bereits bezahlt.")
        if state.next_level is None:
            raise DunningError(f"Für Rechnung {invoice.number} sind alle Mahnstufen ausgeschöpft.")
        level = state.next_level
        t = Lang(invoice.language)
        NumberSequence.objects.get_or_create(company=invoice.company, doc_type=SEQUENCE, defaults={"pattern": PATTERN})
        notice = DunningNotice(
            company=invoice.company,
            invoice=invoice,
            level=level.level,
            title=level.name if t.code == "de" else t.texts["dunning_titles"].get(level.level, level.name),
            number=allocate_number(invoice.company, SEQUENCE, issued_on),
            issued_on=issued_on,
            pay_by=issued_on + dt.timedelta(days=level.pay_days),
            open_amount=state.open_amount,
            fee=level.fee,
            language=t.code,
            created_by=user,
        )
        pdf = render_html_pdf_a3(render_notice_html(notice))
        notice.blob = store(
            pdf, mime="application/pdf", retention_until=retention_end(issued_on, LETTER_RETENTION_YEARS)
        )
        notice.save()
        record(
            "dunning.create",
            actor=user,
            company=invoice.company,
            entity="document",
            entity_id=invoice.pk,
            data={"notice": notice.number, "level": notice.level, "open": str(notice.open_amount)},
        )
    return notice


def default_mail(notice: DunningNotice) -> tuple[str, str]:
    t = Lang(notice.language)
    invoice = notice.invoice
    subject = t("mail_dunning_subject", title=notice.title, number=invoice.number)
    text = t(
        "mail_dunning_text",
        title=notice.title,
        number=invoice.number,
        amount=t.money(notice.total),
        date=t.date(notice.pay_by),
    )
    regards = invoice.company.contact_name or invoice.company.name
    lines = [t("mail_greeting"), "", text, "", t("dunning_ignore"), "", t("mail_regards"), regards]
    return subject, "\n".join(lines)


def send_notice(notice: DunningNotice, *, to: list[str], subject: str, body: str, user=None):
    """E-mail the letter together with the invoice PDF; logged like any document mail."""
    invoice = notice.invoice
    files = [(f"{notice.number}.pdf", load(notice.blob), "application/pdf")]
    pdf = invoice.renditions.filter(blob__mime="application/pdf").select_related("blob").first()
    if pdf:
        files.append((f"{invoice.number}.pdf", load(pdf.blob), "application/pdf"))
    log = send_document(
        invoice,
        to=to,
        subject=subject,
        body=body,
        sent_by=user,
        attachments=files,
        activity_text=f"{notice.title} {notice.number} zu {invoice.number} an {', '.join(to)} gesendet",
    )
    DunningNotice.objects.filter(pk=notice.pk).update(sent_at=log.sent_at)
    return log
