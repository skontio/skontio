"""Automatische Zuordnung: nur eindeutige Zahlungseingänge, standardmäßig aus, zurücknehmbar per Storno."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from skontio.audit.models import AuditEvent
from skontio.banking.models import BankTransaction
from skontio.banking.services import _number_in, auto_match, unmatch
from skontio.payments.models import Payment
from skontio.payments.services import open_amount, paid_amount
from tests.test_banking import invoice

D = Decimal


def tx(shop, amount, purpose, key):
    return BankTransaction.objects.create(
        company=shop.company, booked_on=dt.date(2026, 10, 1), amount=D(amount), purpose=purpose,
        counterparty="Kunde GmbH", import_key=key, source="csv",
    )  # fmt: skip


def switch_on(shop):
    shop.company.auto_match_payments = True
    shop.company.save()


def test_off_by_default(shop):
    inv = invoice(shop)
    tx(shop, "95.20", f"Rechnung {inv.number}", "a")
    assert auto_match(shop.company) == 0 and paid_amount(inv) == 0


def test_books_only_unambiguous_payments(shop):
    switch_on(shop)
    inv = invoice(shop)
    exact = tx(shop, "95.20", f"Rechnung {inv.number} danke", "a")
    partial = tx(shop, "50.00", f"Teilzahlung {inv.number}", "b")
    no_number = tx(shop, "95.20", "Rechnung", "c")
    assert auto_match(shop.company) == 1
    exact.refresh_from_db()
    assert exact.auto_matched and exact.payment.document == inv and open_amount(inv) == 0
    for other in (partial, no_number):
        other.refresh_from_db()
        assert other.payment is None
    assert AuditEvent.objects.filter(action="bank.auto_match").count() == 1


def test_similar_numbers_do_not_match():
    assert _number_in("RE-2026-0001", "Zahlung RE 2026 0001")
    assert not _number_in("RE-2026-1", "Zahlung RE-2026-10")
    assert not _number_in("RE-2026-1", "XRE-2026-1")
    assert not _number_in("", "irgendwas")


def test_two_candidates_or_a_paid_invoice_are_left_alone(shop):
    switch_on(shop)
    first, second = invoice(shop), invoice(shop)
    both = tx(shop, "95.20", f"{first.number} und {second.number}", "a")
    assert auto_match(shop.company) == 0
    one = tx(shop, "95.20", first.number, "b")
    again = tx(shop, "95.20", first.number, "c")  # paid twice: only the first is booked
    assert auto_match(shop.company) == 1
    for row in (both, again):
        row.refresh_from_db()
        assert row.payment is None
    one.refresh_from_db()
    assert one.payment is not None


def test_unmatch_reverses_the_payment_and_reopens(shop):
    switch_on(shop)
    inv = invoice(shop)
    row = tx(shop, "95.20", inv.number, "a")
    auto_match(shop.company)
    row.refresh_from_db()
    unmatch(row)
    row.refresh_from_db()
    assert row.payment is None and not row.auto_matched and open_amount(inv) == D("95.20")
    assert Payment.objects.filter(document=inv).count() == 2  # original and counter-booking stay visible


def test_views_switch_and_take_back(client, shop):
    inv = invoice(shop)
    row = tx(shop, "95.20", inv.number, "a")
    client.force_login(shop.user)
    client.post("/bank/automatik/", {"aktiv": "1"})
    shop.company.refresh_from_db()
    row.refresh_from_db()
    assert shop.company.auto_match_payments and row.auto_matched
    assert "automatisch" in client.get("/bank/?status=alle").content.decode()
    client.post(f"/bank/{row.pk}/rueckgaengig/")
    row.refresh_from_db()
    assert row.payment is None
    client.post("/bank/automatik/", {"aktiv": "0"})
    shop.company.refresh_from_db()
    assert not shop.company.auto_match_payments


def test_only_admins_switch_it_on(client, shop, django_user_model):
    staff_less = django_user_model.objects.create_user("mitarbeit")
    client.force_login(staff_less)
    assert client.post("/bank/automatik/", {"aktiv": "1"}).status_code == 403
    shop.company.refresh_from_db()
    assert not shop.company.auto_match_payments
