"""Dauerauftrag: IBAN lernen, dann automatisch auf die älteste offene Rechnung der Serie – sonst nur Vorschlag."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.banking.models import BankTransaction
from skontio.banking.services import auto_match, book_payment, suggestions, unmatch
from skontio.documents.services import cancel_invoice, finalize
from skontio.payments.models import Payment
from skontio.payments.services import open_amount, record_payment
from skontio.recurring.models import RecurringInvoice
from skontio.recurring.services import create_from_invoice, generate
from tests.test_recurring import source_invoice

D = Decimal
PAYER = "DE89370400440532013000"


def tx(shop, amount, purpose="Webhosting", iban=PAYER, key="a", day=dt.date(2026, 4, 1)):
    return BankTransaction.objects.create(
        company=shop.company, booked_on=day, amount=D(amount), purpose=purpose, counterparty="Kunde GmbH",
        iban=iban, import_key=key, source="csv",
    )  # fmt: skip


@pytest.fixture
def series(shop):
    """Three open monthly invoices of 190,40 € (Jan–Mar); the invoice the series was made from is paid in cash."""
    source = source_invoice(shop)
    record_payment(source, source.gross_total, dt.date(2026, 2, 1), method=Payment.Method.CASH)
    recurring = create_from_invoice(source, name="Webhosting", interval=1, start_date=dt.date(2026, 1, 1))
    invoices = [finalize(draft) for draft in generate(recurring, today=dt.date(2026, 3, 15))]
    shop.company.auto_match_payments = True
    shop.company.save()
    return recurring, invoices


def test_first_payment_teaches_the_iban_then_the_rest_is_automatic(shop, series):
    recurring, (january, february, march) = series
    first = tx(shop, "190.40", key="1")
    assert auto_match(shop.company) == 0  # IBAN not known yet: only a suggestion
    assert [s.target for s in suggestions(first)][:1] == [january]  # oldest open first
    book_payment(first, january, user=shop.user)
    recurring.refresh_from_db()
    assert recurring.payer_iban == PAYER

    second, third = tx(shop, "190.40", key="2", day=dt.date(2026, 5, 1)), tx(shop, "190.40", key="3")
    assert auto_match(shop.company) == 2
    for row, invoice in ((third, february), (second, march)):  # booked in date order onto the oldest open
        row.refresh_from_db()
        assert row.auto_matched and row.payment.document == invoice
    assert all(open_amount(inv) == 0 for inv in (january, february, march))


def test_other_amounts_ibans_or_named_invoices_stay_suggestions(shop, series):
    recurring, (january, february, _) = series
    recurring.payer_iban = PAYER
    recurring.save()
    partial = tx(shop, "100.00", key="p")
    stranger = tx(shop, "190.40", iban="DE02120300000000202051", key="s")
    named = tx(shop, "190.40", purpose=f"Rechnung {february.number} und mehr", key="n")
    double = tx(shop, "380.80", key="d")  # two months at once: no guessing
    assert auto_match(shop.company) == 1  # only "named": invoice number rule, February
    for row in (partial, stranger, double):
        row.refresh_from_db()
        assert row.payment is None
    named.refresh_from_db()
    assert named.payment.document == february and open_amount(january) == D("190.40")


def test_off_switch_and_iban_formats(shop, series):
    recurring, (january, *_) = series
    recurring.payer_iban = PAYER
    recurring.save()
    shop.company.auto_match_payments = False
    shop.company.save()
    row = tx(shop, "190.40", iban="de89 3704 0044 0532 0130 00")
    assert auto_match(shop.company) == 0
    shop.company.auto_match_payments = True
    shop.company.save()
    assert auto_match(shop.company) == 1
    row.refresh_from_db()
    assert row.payment.document == january


def test_learning_only_once_and_not_for_plain_invoices(shop, series):
    recurring, (january, february, _) = series
    book_payment(tx(shop, "190.40", key="1"), january, user=shop.user)
    book_payment(tx(shop, "190.40", iban="DE02120300000000202051", key="2"), february, user=shop.user)
    recurring.refresh_from_db()
    assert recurring.payer_iban == PAYER  # the first payer stays; a change needs "vergessen"
    plain = source_invoice(shop)
    book_payment(tx(shop, "190.40", iban="DE11520513735120710131", key="3"), plain, user=shop.user)
    assert not RecurringInvoice.objects.filter(payer_iban="DE11520513735120710131").exists()


def test_cancelled_invoices_are_skipped_and_undo_works(shop, series):
    recurring, (january, february, _) = series
    recurring.payer_iban = PAYER
    recurring.save()
    cancel_invoice(january, reason="falscher Betrag", issue_date=dt.date(2026, 2, 1))
    row = tx(shop, "190.40")
    auto_match(shop.company)
    row.refresh_from_db()
    assert row.payment.document == february
    unmatch(row, user=shop.user)
    row.refresh_from_db()
    assert row.payment is None and open_amount(february) == D("190.40")


def test_forget_payer_in_the_list(client, shop, series):
    recurring, _ = series
    recurring.payer_iban = PAYER
    recurring.save()
    client.force_login(shop.user)
    assert PAYER in client.get("/wiederkehrend/").content.decode()
    client.post(f"/wiederkehrend/{recurring.pk}/zahler-vergessen/")
    recurring.refresh_from_db()
    assert recurring.payer_iban == ""
    tx(shop, "190.40")
    assert auto_match(shop.company) == 0


def test_paying_the_source_invoice_teaches_the_iban(shop):
    source = source_invoice(shop)
    recurring = create_from_invoice(source, name="Webhosting", interval=1, start_date=dt.date(2026, 2, 1))
    book_payment(tx(shop, str(source.gross_total), key="q"), source, user=shop.user)
    recurring.refresh_from_db()
    assert recurring.payer_iban == PAYER and recurring.source == source
