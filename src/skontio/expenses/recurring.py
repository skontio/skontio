"""Wiederkehrende Ausgaben: due suggestions, taking one over (as a normal expense) or skipping it."""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from skontio.audit.services import record
from skontio.expenses.models import Expense, RecurringExpense
from skontio.expenses.services import ExpenseError
from skontio.recurring.services import add_months

COPIED = ("supplier", "supplier_contact", "supplier_country", "description", "category", "gross_amount",
          "vat_rate", "reverse_charge", "method")  # fmt: skip


def next_due(template: RecurringExpense) -> dt.date | None:
    """Date of the next open period, or None when the template has ended."""
    due = add_months(template.start_date, template.done * template.interval)
    if template.end_date and due > template.end_date:
        return None
    return due


def due_suggestions(company, today: dt.date | None = None) -> list[tuple[RecurringExpense, dt.date]]:
    today = today or timezone.localdate()
    found = []
    for template in RecurringExpense.objects.filter(company=company, active=True):
        due = next_due(template)
        if due is not None and due <= today:
            found.append((template, due))
    return sorted(found, key=lambda pair: (pair[1], pair[0].name))


def from_expense(expense: Expense, *, interval: int, user=None) -> RecurringExpense:
    """A template from an existing expense; its first suggestion is one interval after the receipt date."""
    if interval not in RecurringExpense.Interval.values:
        raise ExpenseError("Unbekannter Rhythmus.")
    if expense.cancelled_at:
        raise ExpenseError("Aus einer stornierten Ausgabe lässt sich keine Vorlage machen.")
    template = RecurringExpense.objects.create(
        company=expense.company,
        name=(expense.description or expense.supplier)[:120],
        start_date=add_months(expense.receipt_date, interval),
        interval=interval,
        source=expense,
        created_by=user,
        **{field: getattr(expense, field) for field in COPIED},
    )
    record("recurring_expense.create", actor=user, company=expense.company, entity="recurring_expense",
           entity_id=template.pk, data={"from": expense.number, "interval": interval})  # fmt: skip
    return template


def prefill(template: RecurringExpense, due: dt.date) -> dict:
    return {field: getattr(template, field) for field in COPIED} | {"receipt_date": due, "paid_on": due}


def mark_done(template: RecurringExpense, due: dt.date, *, user=None, action: str) -> bool:
    """Count the period ``due`` as handled – only if it is still the open one (two tabs, double clicks)."""
    with transaction.atomic():
        locked = RecurringExpense.objects.select_for_update().get(pk=template.pk)
        if next_due(locked) != due:
            return False
        locked.done += 1
        locked.save(update_fields=["done"])
        record(f"recurring_expense.{action}", actor=user, company=locked.company, entity="recurring_expense",
               entity_id=locked.pk, data={"due": due.isoformat()})  # fmt: skip
    template.done = locked.done
    return True


def toggle(template: RecurringExpense, *, user=None) -> RecurringExpense:
    template.active = not template.active
    template.save(update_fields=["active"])
    record("recurring_expense.toggle", actor=user, company=template.company, entity="recurring_expense",
           entity_id=template.pk, data={"active": template.active})  # fmt: skip
    return template
