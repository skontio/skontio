"""Postausgang: alle versendeten Mails, Filter, CSV ohne Formel-Injektion, fremde Firmen unsichtbar."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.mail.models import MailLog
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime

DAY = dt.date(2026, 3, 2)


def invoice(company, customer, item):
    draft = create_draft(company, Document.Type.INVOICE, customer, issue_date=DAY, service_date=DAY)
    add_line_from_item(draft, item, quantity=Decimal("1"))
    return finalize(draft)


def log(doc, *, to="a@kunde.example", subject="Rechnung", status=MailLog.Status.SENT, when=None, **extra):
    entry = MailLog.objects.create(document=doc, to=[to], subject=subject, status=status, **extra)
    if when:
        MailLog.objects.filter(pk=entry.pk).update(sent_at=when)
    return entry


@pytest.fixture
def doc(shop):
    return invoice(shop.company, shop.org, shop.item)


def test_lists_sent_and_failed_mails(client, shop, doc):
    log(doc, subject="Ihre Rechnung", attachments=[{"name": "RE-1.pdf", "sha256": "x"}])
    log(doc, subject="Erneut", status=MailLog.Status.FAILED, error="Verbindung abgelehnt")
    client.force_login(shop.user)
    page = client.get("/postausgang/").content.decode()
    assert "Ihre Rechnung" in page and "RE-1.pdf" in page and "Fehlgeschlagene Mails: 1" in page
    failed = client.get("/postausgang/", {"status": "fehlgeschlagen"}).content.decode()
    assert "Erneut" in failed and "Ihre Rechnung" not in failed


def test_search_by_recipient_subject_and_number(client, shop, doc):
    log(doc, to="buchhaltung@kunde.example", subject="Alpha")
    log(doc, to="chef@woanders.example", subject="Beta")
    client.force_login(shop.user)
    page = client.get("/postausgang/", {"q": "buchhaltung"}).content.decode()
    assert "Alpha" in page and "Beta" not in page
    assert "Alpha" in client.get("/postausgang/", {"q": doc.number}).content.decode()


def test_period_filter_uses_whole_days(client, shop, doc):
    log(doc, subject="Spätabends", when=timezone.make_aware(dt.datetime(2026, 3, 5, 23, 59)))
    log(doc, subject="Vorher", when=timezone.make_aware(dt.datetime(2026, 2, 1, 9, 0)))
    client.force_login(shop.user)
    page = client.get("/postausgang/", {"von": "2026-03-05", "bis": "2026-03-05"}).content.decode()
    assert "Spätabends" in page and "Vorher" not in page


def test_other_company_mails_stay_hidden(client, shop):
    other = Company.objects.create(
        name="Fremd", street="X", postcode="1", city="Y", tax_number="9/9/9", iban="DE02120300000000202051"
    )
    start_regime(other, Regime.STANDARD, dt.date(2024, 1, 1))
    foreign = invoice(
        other,
        create_customer(other, name="Fremdkunde", street="X", postcode="1", city="Y"),
        create_item(other, name="X", unit="LS", net_price=Decimal("1")),
    )
    log(foreign, subject="Geheim")
    client.force_login(shop.user)
    assert "Geheim" not in client.get("/postausgang/").content.decode()
    assert "Geheim" not in client.get("/postausgang/", {"csv": "1"}).content.decode("utf-8-sig")


def test_csv_is_formula_safe(client, shop, doc):
    log(doc, to="=HYPERLINK(1)@x.example", subject="+cmd|calc", error="@SUM(1)", status=MailLog.Status.FAILED)
    client.force_login(shop.user)
    response = client.get("/postausgang/", {"csv": "1", "status": "fehlgeschlagen"})
    text = response.content.decode("utf-8-sig")
    assert response["Content-Disposition"].startswith('attachment; filename="postausgang-')
    assert "'=HYPERLINK" in text and "'+cmd" in text and "'@SUM" in text
    assert ";=" not in text and ";+" not in text and ";@" not in text


def test_needs_login(client, shop):
    assert client.get("/postausgang/").status_code == 302


@pytest.mark.parametrize(
    "params",
    [{"von": "2026-02-30"}, {"bis": "<script>"}, {"status": "'; DROP TABLE"}, {"q": "%" * 500}, {"q": "\x00"}],
)
def test_garbage_parameters_never_crash(client, shop, doc, params):
    log(doc, subject="<script>alert(1)</script>")
    client.force_login(shop.user)
    response = client.get("/postausgang/", params)
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.content.decode()
