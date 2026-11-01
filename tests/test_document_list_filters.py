"""Dokumentliste: Filter nach Status und Zeitraum, CSV-Export ohne Formel-Injektion, Müll-Parameter."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize

DAY = dt.date(2026, 3, 2)


def make(shop, customer, finalized=True):
    draft = create_draft(shop.company, Document.Type.INVOICE, customer, issue_date=DAY, service_date=DAY)
    add_line_from_item(draft, shop.item, quantity=Decimal("1"))
    return finalize(draft) if finalized else draft


def test_status_and_period_filters(client, shop):
    client.force_login(shop.user)
    invoice, draft = make(shop, shop.org), make(shop, shop.org, finalized=False)
    page = client.get("/dokumente/", {"status": "entwurf"}).content.decode()
    assert f"/dokumente/{draft.pk}/" in page and f"/dokumente/{invoice.pk}/" not in page
    assert invoice.number not in client.get("/dokumente/", {"von": "2026-04-01"}).content.decode()


def test_csv_export_is_formula_safe(client, shop):
    evil = create_customer(shop.company, name="=1+1", street="W", postcode="1", city="K")
    make(shop, evil)
    response = client.get("/dokumente/", {"csv": "1"}) if client.force_login(shop.user) is None else None
    text = response.content.decode("utf-8-sig")
    assert response["Content-Disposition"].startswith('attachment; filename="dokumente-')
    assert "'=1+1" in text and ";=1+1" not in text


def test_garbage_parameters_never_crash(client, shop):
    client.force_login(shop.user)
    for params in ({"von": "2026-02-30"}, {"bis": "<script>"}, {"status": "x"}, {"kunde": "-1"}, {"q": "A" * 5000}):
        assert client.get("/dokumente/", params).status_code == 200
