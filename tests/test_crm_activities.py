"""Aktivitäten und Wiedervorlagen im CRM."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

from skontio.crm.activities import ActivityError, complete_follow_up, create_follow_up, due_follow_ups, log_activity
from skontio.crm.models import Activity, FollowUp
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line, create_draft, finalize

TODAY = dt.date(2026, 10, 12)


def test_follow_ups_due_within_a_week(shop):
    create_follow_up(shop.org, title="überfällig", due_on=TODAY - dt.timedelta(days=3))
    create_follow_up(shop.org, title="heute", due_on=TODAY)
    create_follow_up(shop.org, title="bald", due_on=TODAY + dt.timedelta(days=7))
    create_follow_up(shop.org, title="später", due_on=TODAY + dt.timedelta(days=8))
    done = create_follow_up(shop.org, title="erledigt", due_on=TODAY)
    complete_follow_up(done, user=shop.user, note="Kunde hat zugesagt")
    assert [f.title for f in due_follow_ups(shop.company, TODAY)] == ["überfällig", "heute", "bald"]
    assert Activity.objects.get().text == "Wiedervorlage erledigt: erledigt – Kunde hat zugesagt"


def test_activity_needs_text_and_matching_document(shop):
    with pytest.raises(ActivityError):
        log_activity(shop.org, text="  ")
    other = create_customer(shop.company, name="Andere")
    document = create_draft(shop.company, Document.Type.QUOTE, other, issue_date=TODAY)
    with pytest.raises(ActivityError):
        create_follow_up(shop.org, title="x", due_on=TODAY, document=document)


def test_customer_page_activity_and_follow_up(client, shop):
    client.force_login(shop.user)
    client.post(f"/kunden/{shop.org.pk}/aktivitaeten/", {"kind": "call", "text": "Rückruf wegen Angebot"})
    client.post(f"/kunden/{shop.org.pk}/wiedervorlagen/", {"title": "Nachfassen", "due_on": "2026-10-20"})
    page = client.get(f"/kunden/{shop.org.pk}/").content.decode()
    assert "Rückruf wegen Angebot" in page and "Anruf" in page and "Nachfassen" in page
    follow_up = FollowUp.objects.get()
    response = client.post(f"/wiedervorlagen/{follow_up.pk}/erledigt/", {"back": "//evil.example/"})
    assert response["Location"] == "/"
    follow_up.refresh_from_db()
    assert follow_up.done_at is not None


def test_dashboard_lists_due_follow_ups(client, shop):
    create_follow_up(shop.org, title="Angebot nachfassen", due_on=timezone.localdate())
    client.force_login(shop.user)
    assert "Angebot nachfassen" in client.get("/").content.decode()


def test_quote_follow_up_from_document_page(client, shop):
    quote = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=TODAY)
    add_line(quote, text="x", quantity=Decimal("1"), unit_code="HUR", unit_price=Decimal("10"))
    quote = finalize(quote)
    client.force_login(shop.user)
    page = client.get(f"/dokumente/{quote.pk}/").content.decode()
    assert f"Angebot {quote.number} nachfassen" in page
    response = client.post(
        f"/kunden/{shop.org.pk}/wiedervorlagen/",
        {"title": "Nachfassen", "due_on": "2026-10-26", "document": quote.pk, "back": f"/dokumente/{quote.pk}/"},
    )
    assert response["Location"] == f"/dokumente/{quote.pk}/"
    assert FollowUp.objects.get().document == quote


def test_sending_a_document_logs_an_activity(shop, settings):
    from skontio.mail.services import send_document

    settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    document = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=TODAY)
    add_line(document, text="x", quantity=Decimal("1"), unit_code="HUR", unit_price=Decimal("10"))
    document = finalize(document)
    send_document(document, to=["a@kunde.example"], subject="Angebot", body="Hallo", sent_by=shop.user)
    assert len(mail.outbox) == 1
    activity = Activity.objects.get()
    assert activity.kind == "email" and activity.document == document and "a@kunde.example" in activity.text


def test_follow_up_date_field_is_prefilled_in_iso_format(client, shop):
    """<input type="date"> only accepts YYYY-MM-DD; a German date would show an empty field."""
    import datetime as dt

    from django.utils import timezone

    client.force_login(shop.user)
    page = client.get(f"/kunden/{shop.org.pk}/").content.decode()
    assert f'value="{(timezone.localdate() + dt.timedelta(days=7)).isoformat()}"' in page
