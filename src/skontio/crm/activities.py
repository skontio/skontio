"""Customer activities and follow-ups (Wiedervorlagen)."""

from __future__ import annotations

import datetime as dt

from django.utils import timezone

from skontio.crm.models import Activity, Customer, FollowUp

UPCOMING_DAYS = 7


class ActivityError(ValueError):
    pass


def log_activity(
    customer: Customer, *, text: str, kind: str = Activity.Kind.NOTE, occurred_at=None, document=None, user=None
) -> Activity:
    if not text.strip():
        raise ActivityError("Bitte einen Text eingeben")
    if document is not None and document.customer_id != customer.pk:
        raise ActivityError("Das Dokument gehört zu einem anderen Kunden")
    return Activity.objects.create(
        customer=customer,
        kind=kind,
        text=text.strip(),
        occurred_at=occurred_at or timezone.now(),
        document=document,
        created_by=user,
    )


def create_follow_up(customer: Customer, *, title: str, due_on: dt.date, document=None, user=None) -> FollowUp:
    if not title.strip():
        raise ActivityError("Bitte einen Titel eingeben")
    if document is not None and document.customer_id != customer.pk:
        raise ActivityError("Das Dokument gehört zu einem anderen Kunden")
    return FollowUp.objects.create(
        customer=customer, title=title.strip(), due_on=due_on, document=document, created_by=user
    )


def complete_follow_up(follow_up: FollowUp, *, user=None, note: str = "") -> FollowUp:
    if follow_up.done_at is None:
        follow_up.done_at = timezone.now()
        follow_up.save(update_fields=["done_at"])
        log_activity(
            follow_up.customer,
            text=f"Wiedervorlage erledigt: {follow_up.title}" + (f" – {note.strip()}" if note.strip() else ""),
            document=follow_up.document,
            user=user,
        )
    return follow_up


def due_follow_ups(company, today: dt.date, *, days: int = UPCOMING_DAYS):
    """Open follow-ups that are overdue, due today or due within ``days``."""
    return (
        FollowUp.objects.filter(customer__company=company, done_at__isnull=True, due_on__lte=today + dt.timedelta(days))
        .select_related("customer", "document")
        .order_by("due_on", "pk")
    )
