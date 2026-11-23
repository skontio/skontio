"""Wiederkehrende Rechnungen: Zeiträume, Nachholen, keine Doppelten, Entwürfe statt Versand."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.core.management import call_command

from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.recurring.models import RecurringInvoice, RecurringRun
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
        assert draft.origin == Document.Origin.RECURRING and not draft.ai_review_required
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


class TestEditAndDelete:
    def post_edit(self, client, recurring, **overrides):
        lines = list(recurring.lines.order_by("position"))
        data = {
            "name": recurring.name,
            "interval": str(recurring.interval),
            "start_date": recurring.start_date.isoformat(),
            "in_advance": "on",
            "intro_text": "Neu",
            "closing_text": "",
            "form-TOTAL_FORMS": str(len(lines) + 1),
            "form-INITIAL_FORMS": str(len(lines)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for i, line in enumerate(lines):
            data |= {
                f"form-{i}-id": str(line.pk),
                f"form-{i}-text": line.text,
                f"form-{i}-quantity": str(line.quantity),
                f"form-{i}-unit_code": line.unit_code,
                f"form-{i}-unit_price": str(line.unit_price),
                f"form-{i}-tax_treatment": line.tax_treatment,
            }
        n = len(lines)
        data |= {f"form-{n}-quantity": "1", f"form-{n}-unit_code": "C62", f"form-{n}-tax_treatment": "standard"}
        data |= overrides
        return client.post(f"/wiederkehrend/{recurring.pk}/bearbeiten/", data)

    def test_edit_page_and_header_change(self, client, shop):
        recurring = create_from_invoice(source_invoice(shop), name="X", interval=1, start_date=dt.date(2027, 1, 1))
        client.force_login(shop.user)
        assert "Positionen" in client.get(f"/wiederkehrend/{recurring.pk}/bearbeiten/").content.decode()
        response = self.post_edit(client, recurring, name="Neu", interval="3")
        assert response["Location"] == "/wiederkehrend/"
        recurring.refresh_from_db()
        assert (recurring.name, recurring.interval, recurring.intro_text) == ("Neu", 3, "Neu")

    def test_remove_and_add_lines(self, client, shop):
        invoice = source_invoice(shop)
        recurring = create_from_invoice(invoice, name="X", interval=1, start_date=dt.date(2027, 1, 1))
        client.force_login(shop.user)
        n = recurring.lines.count()
        added = {f"form-{n}-text": "Zusatz", f"form-{n}-unit_price": "10", f"form-{n}-quantity": "1"}
        self.post_edit(client, recurring, **added)
        assert list(recurring.lines.order_by("position").values_list("text", flat=True))[-1] == "Zusatz"
        first = recurring.lines.order_by("position").first()
        self.post_edit(client, recurring, **{"form-0-DELETE": "on"})
        assert not recurring.lines.filter(pk=first.pk).exists() and recurring.lines.count() == n
        assert recurring.lines.get().position == 1

    def test_cannot_remove_every_line(self, client, shop):
        recurring = create_from_invoice(source_invoice(shop), name="X", interval=1, start_date=dt.date(2027, 1, 1))
        client.force_login(shop.user)
        response = self.post_edit(client, recurring, **{"form-0-DELETE": "on"})
        assert response.status_code == 200 and recurring.lines.count() == 1

    def test_schedule_is_locked_after_first_invoice(self, client, shop):
        recurring = create_from_invoice(source_invoice(shop), name="X", interval=1, start_date=dt.date(2026, 1, 1))
        generate(recurring, today=dt.date(2026, 1, 1))
        client.force_login(shop.user)
        self.post_edit(client, recurring, name="Neu", interval="12", start_date="2030-01-01")
        recurring.refresh_from_db()
        assert (recurring.name, recurring.interval, recurring.start_date) == ("Neu", 1, dt.date(2026, 1, 1))

    def test_delete_only_without_runs(self, client, shop):
        client.force_login(shop.user)
        fresh = create_from_invoice(source_invoice(shop), name="A", interval=1, start_date=dt.date(2027, 1, 1))
        client.post(f"/wiederkehrend/{fresh.pk}/loeschen/")
        assert not RecurringInvoice.objects.filter(pk=fresh.pk).exists()
        used = create_from_invoice(source_invoice(shop), name="B", interval=1, start_date=dt.date(2026, 1, 1))
        generate(used, today=dt.date(2026, 1, 1))
        client.post(f"/wiederkehrend/{used.pk}/loeschen/")
        assert RecurringInvoice.objects.filter(pk=used.pk).exists()


class TestAutoSend:
    @pytest.fixture(autouse=True)
    def _smtp(self, settings):
        settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
        settings.DEFAULT_FROM_EMAIL = "rechnung@example.org"

    def run(self, shop, **options):
        from skontio.recurring.services import generate_due

        r = create_from_invoice(
            source_invoice(shop), name="Hosting", interval=1, start_date=dt.date(2026, 3, 1), **options
        )
        return r, generate_due(shop.company, today=dt.date(2026, 3, 1), auto_send=True)

    def test_manual_series_mails_admins_that_a_draft_waits(self, shop):
        from django.core import mail

        shop.user.email = "inhaber@example.org"
        shop.user.save()
        _, drafts = self.run(shop)  # auto_send off
        drafts[0].refresh_from_db()
        assert drafts[0].status == Document.Status.DRAFT
        (note,) = mail.outbox
        assert "zum Festschreiben" in note.subject and f"/dokumente/{drafts[0].pk}/" in note.body
        assert note.to == ["inhaber@example.org"]

    def test_finalizes_and_mails_when_switched_on(self, shop):
        from django.core import mail

        _, drafts = self.run(shop, auto_send=True)
        drafts[0].refresh_from_db()
        assert drafts[0].status == Document.Status.FINALIZED
        assert [m.to for m in mail.outbox] == [["a@kunde.example"]]

    def test_stays_a_draft_by_default(self, shop):
        from django.core import mail

        _, drafts = self.run(shop)
        drafts[0].refresh_from_db()
        assert drafts[0].status == Document.Status.DRAFT
        assert mail.outbox == []

    def test_web_style_run_never_sends(self, shop):
        from django.core import mail

        from skontio.recurring.services import generate_due

        create_from_invoice(
            source_invoice(shop), name="Hosting", interval=1, start_date=dt.date(2026, 3, 1), auto_send=True
        )
        drafts = generate_due(shop.company, today=dt.date(2026, 3, 1))
        drafts[0].refresh_from_db()
        assert drafts[0].status == Document.Status.DRAFT
        assert mail.outbox == []

    def test_customer_without_email_cannot_be_switched_on(self, shop):
        shop.org.email = ""
        shop.org.save()
        with pytest.raises(RecurringError):
            create_from_invoice(
                source_invoice(shop), name="Hosting", interval=1, start_date=dt.date(2026, 3, 1), auto_send=True
            )

    def test_missing_email_later_leaves_the_draft(self, shop):
        from django.core import mail

        r = create_from_invoice(
            source_invoice(shop), name="Hosting", interval=1, start_date=dt.date(2026, 3, 1), auto_send=True
        )
        shop.org.email = ""
        shop.org.save()
        from skontio.recurring.services import generate_due

        drafts = generate_due(shop.company, today=dt.date(2026, 3, 1), auto_send=True)
        drafts[0].refresh_from_db()
        assert drafts[0].status == Document.Status.DRAFT
        assert mail.outbox == []
        assert r.runs.count() == 1
