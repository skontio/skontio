"""Wiederkehrende Ausgaben: Vorschlag zum Termin, übernehmen, überspringen, pausieren – nie doppelt, nie fremd."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from skontio.core.models import Company
from skontio.expenses.models import Expense, RecurringExpense
from skontio.expenses.recurring import due_suggestions, from_expense, mark_done, next_due
from skontio.expenses.services import ExpenseError, cancel_expense, create_expense
from skontio.recurring.services import add_months

TODAY = timezone.localdate()


def rent(company, day=None):
    day = day or add_months(TODAY, -1)
    return create_expense(
        company, receipt_date=day, paid_on=day, supplier="Vermieter", description="Miete Büro",
        gross_amount=Decimal("500.00"), vat_rate=Decimal("0"), category=Expense.Category.RENT,
    )  # fmt: skip


@pytest.fixture
def template(shop):
    return from_expense(rent(shop.company), interval=1, user=shop.user)


def test_template_starts_one_interval_after_the_receipt(shop, template):
    assert template.start_date == add_months(add_months(TODAY, -1), 1)
    assert (template.name, template.gross_amount, template.category) == ("Miete Büro", Decimal("500.00"), "rent")
    assert due_suggestions(shop.company) == [(template, template.start_date)]


def test_not_due_before_its_date_and_ends_with_end_date(shop, template):
    assert due_suggestions(shop.company, today=template.start_date - dt.timedelta(days=1)) == []
    template.end_date = template.start_date
    template.save()
    assert mark_done(template, template.start_date, action="taken")
    assert next_due(template) is None and due_suggestions(shop.company, today=add_months(TODAY, 24)) == []


def test_mark_done_only_once_per_period(shop, template):
    due = template.start_date
    assert mark_done(template, due, action="taken")
    assert not mark_done(template, due, action="taken")  # second tab / double click
    assert RecurringExpense.objects.get().done == 1


def test_missed_periods_are_suggested_one_after_another(shop):
    old = from_expense(rent(shop.company, add_months(TODAY, -4)), interval=1)
    assert due_suggestions(shop.company)[0][1] == add_months(add_months(TODAY, -4), 1)
    mark_done(old, next_due(old), action="skipped")
    assert due_suggestions(shop.company)[0][1] == add_months(add_months(TODAY, -4), 2)


@pytest.mark.parametrize("interval", [0, 2, 5, 13, -1, 10**6])
def test_unknown_interval_is_refused(shop, interval):
    with pytest.raises(ExpenseError):
        from_expense(rent(shop.company), interval=interval)


def test_cancelled_expense_cannot_become_a_template(shop):
    expense = rent(shop.company)
    cancel_expense(expense, reason="falsch")
    with pytest.raises(ExpenseError):
        from_expense(expense, interval=1)


# ---- Oberfläche --------------------------------------------------------------------------------


def test_take_over_prefills_and_advances_after_saving(client, shop, template):
    client.force_login(shop.user)
    due = template.start_date.isoformat()
    assert "Fällige wiederkehrende Ausgaben" in client.get("/ausgaben/").content.decode()
    page = client.get("/ausgaben/neu/", {"vorlage": template.pk, "faellig": due}).content.decode()
    assert "Vermieter" in page and "Miete Büro" in page and 'name="vorlage"' in page
    form = {
        "receipt_date": due, "paid_on": due, "supplier": "Vermieter", "gross_amount": "500,00", "vat_rate": "0",
        "category": "rent", "method": "bank", "supplier_country": "DE", "vorlage": template.pk, "faellig": due,
    }  # fmt: skip
    assert client.post("/ausgaben/neu/", form).status_code == 302
    assert client.post("/ausgaben/neu/", form).status_code == 302  # a resent form books, but does not advance twice
    template.refresh_from_db()
    assert template.done == 1 and Expense.objects.filter(supplier="Vermieter").count() == 3


def test_skip_and_pause(client, shop, template):
    client.force_login(shop.user)
    client.post(f"/ausgaben/vorlagen/{template.pk}/ueberspringen/", {"faellig": template.start_date.isoformat()})
    template.refresh_from_db()
    assert template.done == 1 and Expense.objects.count() == 1
    client.post(f"/ausgaben/vorlagen/{template.pk}/umschalten/")
    template.refresh_from_db()
    assert not template.active and due_suggestions(shop.company, today=add_months(TODAY, 12)) == []


def test_create_template_through_the_ui(client, shop):
    expense = rent(shop.company)
    client.force_login(shop.user)
    assert client.post(f"/ausgaben/{expense.pk}/wiederkehrend/", {"interval": "3"}).status_code == 302
    assert RecurringExpense.objects.get().interval == 3
    for bad in ("", "abc", "99999999999999999999", "7"):
        client.post(f"/ausgaben/{expense.pk}/wiederkehrend/", {"interval": bad})
    assert RecurringExpense.objects.count() == 1


@pytest.mark.parametrize(
    "params",
    [{"vorlage": "abc", "faellig": "2026-01-01"}, {"vorlage": "99999999999999999999", "faellig": "x"},
     {"vorlage": "1", "faellig": "2026-02-30"}, {"vorlage": "-1"}],
)  # fmt: skip
def test_garbage_template_parameters_are_ignored(client, shop, template, params):
    client.force_login(shop.user)
    assert client.get("/ausgaben/neu/", params).status_code == 200
    for value in params.values():
        client.post(f"/ausgaben/vorlagen/{template.pk}/ueberspringen/", {"faellig": value})
    template.refresh_from_db()
    assert template.done == 0


def test_wrong_due_date_or_foreign_template_does_nothing(client, shop, template):
    other = Company.objects.create(name="Fremd", street="X", postcode="1", city="Y")
    foreign = from_expense(rent(other), interval=1)
    client.force_login(shop.user)
    later = add_months(template.start_date, 1).isoformat()
    client.post(f"/ausgaben/vorlagen/{template.pk}/ueberspringen/", {"faellig": later})
    assert client.post(f"/ausgaben/vorlagen/{foreign.pk}/ueberspringen/", {"faellig": "2026-01-01"}).status_code == 404
    assert client.post(f"/ausgaben/vorlagen/{foreign.pk}/umschalten/").status_code == 404
    page = client.get("/ausgaben/neu/", {"vorlage": foreign.pk, "faellig": foreign.start_date.isoformat()})
    assert "Vermieter" not in page.content.decode()
    template.refresh_from_db()
    foreign.refresh_from_db()
    assert template.done == 0 and foreign.active and foreign.done == 0
