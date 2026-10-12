"""Wiederkehrende Rechnungen: Zeiträume, Nachholen, keine Doppelten, Entwürfe statt Versand."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.core.management import call_command

from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.recurring.models import RecurringRun
from skontio.recurring.services import (
    RecurringError,
    add_months,
    create_from_invoice,
    due,
    generate,
    next_issue_date,
    period,
)

D = Decimal


def source_invoice(shop):
    doc = create_draft(
        shop.company,
        Document.Type.INVOICE,
        shop.org,
        issue_date=dt.date(2026, 1, 31),
        service_date=dt.date(2026, 1, 31),
        intro_text="Wartung",
    )
    add_line_from_item(doc, shop.item, quantity=D("2"))
    return finalize(doc)


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (dt.date(2026, 1, 31), 1, dt.date(2026, 2, 28)),
        (dt.date(2028, 1, 31), 1, dt.date(2028, 2, 29)),
        (dt.date(2026, 11, 15), 3, dt.date(2027, 2, 15)),
        (dt.date(2026, 1, 1), 12, dt.date(2027, 1, 1)),
    ],
)
def test_add_months(day, months, expected):
    assert add_months(day, months) == expected


class TestPeriods:
    def test_monthly_periods_do_not_drift(self, shop):
        r = create_from_invoice(source_invoice(shop), name="Wartung", interval=1, start_date=dt.date(2026, 1, 31))
        assert period(r, 0) == (dt.date(2026, 1, 31), dt.date(2026, 2, 27))
        assert period(r, 1) == (dt.date(2026, 2, 28), dt.date(2026, 3, 30))
        assert period(r, 2) == (dt.date(2026, 3, 31), dt.date(2026, 4, 29))

    def test_in_advance_or_in_arrears(self, shop):
        invoice = source_invoice(shop)
        advance = create_from_invoice(invoice, name="A", interval=3, start_date=dt.date(2026, 4, 1))
        arrears = create_from_invoice(invoice, name="B", interval=3, start_date=dt.date(2026, 4, 1), in_advance=False)
        assert next_issue_date(advance) == dt.date(2026, 4, 1)
        assert next_issue_date(arrears) == dt.date(2026, 7, 1)


class TestGenerate:
    def test_drafts_with_service_period_and_lines(self, shop):
        recurring = create_from_invoice(
            source_invoice(shop), name="Hosting", interval=1, start_date=dt.date(2026, 3, 1)
        )
        (draft,) = generate(recurring, today=dt.date(2026, 3, 1))
        assert draft.status == Document.Status.DRAFT and draft.issue_date == dt.date(2026, 3, 1)
        assert (draft.service_period_start, draft.service_period_end) == (dt.date(2026, 3, 1), dt.date(2026, 3, 31))
        assert draft.intro_text == "Wartung" and draft.lines.get().quantity == D("2")
        finalize(draft)  # a normal invoice from here on

    def test_missed_periods_are_caught_up_once(self, shop):
        recurring = create_from_invoice(
            source_invoice(shop), name="Hosting", interval=1, start_date=dt.date(2026, 1, 1)
        )
        drafts = generate(recurring, today=dt.date(2026, 3, 15))
        assert [d.service_period_start.month for d in drafts] == [1, 2, 3]
        assert generate(recurring, today=dt.date(2026, 3, 15)) == []
        assert RecurringRun.objects.count() == 3

    def test_end_date_shortens_the_last_period_and_stops(self, shop):
        recurring = create_from_invoice(
            source_invoice(shop),
            name="Projekt",
            interval=1,
            start_date=dt.date(2026, 1, 1),
            end_date=dt.date(2026, 2, 14),
        )
        drafts = generate(recurring, today=dt.date(2026, 12, 31))
        assert [(d.service_period_start, d.service_period_end) for d in drafts] == [
            (dt.date(2026, 1, 1), dt.date(2026, 1, 31)),
            (dt.date(2026, 2, 1), dt.date(2026, 2, 14)),
        ]
        recurring.refresh_from_db()
        assert next_issue_date(recurring) is None

    def test_paused_contracts_are_not_due(self, shop):
        recurring = create_from_invoice(source_invoice(shop), name="X", interval=1, start_date=dt.date(2026, 1, 1))
        assert due(shop.company, dt.date(2026, 1, 1)) == [recurring]
        recurring.active = False
        recurring.save()
        assert due(shop.company, dt.date(2026, 1, 1)) == []

    def test_invalid_input(self, shop):
        invoice = source_invoice(shop)
        with pytest.raises(RecurringError, match="Rhythmus"):
            create_from_invoice(invoice, name="X", interval=5, start_date=dt.date(2026, 1, 1))
        with pytest.raises(RecurringError, match="Ende"):
            create_from_invoice(
                invoice, name="X", interval=1, start_date=dt.date(2026, 2, 1), end_date=dt.date(2026, 1, 1)
            )
        quote = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=dt.date(2026, 1, 1))
        with pytest.raises(RecurringError, match="Nur Rechnungen"):
            create_from_invoice(quote, name="X", interval=1, start_date=dt.date(2026, 1, 1))

    def test_management_command(self, shop):
        create_from_invoice(source_invoice(shop), name="X", interval=12, start_date=dt.date(2020, 1, 1))
        out = io.StringIO()
        call_command("recurring", stdout=out)
        assert "angelegt" in out.getvalue() and RecurringRun.objects.exists()


class TestWeb:
    def test_create_from_invoice_page_and_generate(self, client, shop):
        invoice = source_invoice(shop)
        client.force_login(shop.user)
        page = client.get(f"/dokumente/{invoice.pk}/").content.decode()
        assert "Als wiederkehrend anlegen" in page and 'value="2026-02-01"' in page
        response = client.post(
            f"/wiederkehrend/aus-rechnung/{invoice.pk}/",
            {"name": "Wartung Kunde", "interval": "1", "start_date": "2026-02-01", "in_advance": "on"},
        )
        assert response["Location"] == "/wiederkehrend/"
        listing = client.get("/wiederkehrend/").content.decode()
        assert "Wartung Kunde" in listing and "fällig" in listing
        assert "wiederkehrende Rechnung" in client.get("/").content.decode()
        client.post("/wiederkehrend/erzeugen/")
        assert Document.objects.filter(status=Document.Status.DRAFT, doc_type="invoice").exists()

    def test_pause(self, client, shop):
        recurring = create_from_invoice(source_invoice(shop), name="X", interval=1, start_date=dt.date(2026, 1, 1))
        client.force_login(shop.user)
        client.post(f"/wiederkehrend/{recurring.pk}/umschalten/")
        recurring.refresh_from_db()
        assert not recurring.active
