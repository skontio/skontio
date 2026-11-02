"""Schnellsuche: findet Kontakte, Dokumente, Ausgaben, Leistungen – nur der eigenen Firma, ohne Absturz bei Müll."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line, create_draft, finalize
from skontio.expenses.models import Expense
from skontio.expenses.services import create_expense
from skontio.web.search_views import GROUPS, search

DAY = dt.date(2026, 3, 2)


def expense(company, **kwargs):
    fields = {
        "receipt_date": DAY,
        "supplier": "Hosting GmbH",
        "gross_amount": Decimal("11.90"),
        "vat_rate": Decimal("19"),
        "category": Expense.Category.SOFTWARE,
    } | kwargs
    return create_expense(company, **fields)


def line(doc, text="Pflege", description=""):
    add_line(doc, text=text, description=description, quantity=Decimal("1"), unit_code="LS", unit_price=Decimal("5"))


def nothing(found) -> bool:
    return not any(found[group] for group in GROUPS)


def test_finds_every_kind_by_its_fields(shop):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=DAY, service_date=DAY)
    line(draft, text="Webseite Relaunch", description="inkl. Hosting-Umzug")
    invoice = finalize(draft)
    cost = expense(shop.company, description="Serverkosten März")

    assert search(shop.company, "kunde gmbh")["customers"] == [shop.org]
    assert [d for d, _ in search(shop.company, "relaunch")["documents"]] == [invoice]
    assert [d for d, _ in search(shop.company, "umzug")["documents"]] == [invoice]
    assert [d for d, _ in search(shop.company, invoice.number)["documents"]] == [invoice]
    assert [d for d, _ in search(shop.company, "Kunde GmbH")["documents"]] == [invoice]
    assert search(shop.company, "serverkosten")["expenses"] == [cost]
    assert search(shop.company, "hosting")["expenses"] == [cost]
    assert search(shop.company, "berat")["items"] == [shop.item]


def test_one_document_with_many_matching_lines_is_listed_once(shop):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=DAY, service_date=DAY)
    for _ in range(3):
        line(draft)
    assert len(search(shop.company, "pflege")["documents"]) == 1


def test_other_companies_stay_invisible(shop):
    other = Company.objects.create(name="Fremd", street="X", postcode="1", city="Y")
    create_customer(other, name="Geheimkunde AG", street="X", postcode="1", city="Y")
    expense(other, supplier="Geheimlieferant")
    assert nothing(search(shop.company, "geheim"))


@pytest.mark.parametrize("query", ["", " ", "a", "\t\n"])
def test_too_short_queries_find_nothing(shop, query):
    assert nothing(search(shop.company, query))


def test_long_query_is_cut(shop):
    assert len(search(shop.company, "x" * 50_000)["q"]) == 100


def test_like_wildcards_are_literal(shop):
    """'%' and '_' must not act as wildcards – otherwise "%%" would list every contact."""
    assert nothing(search(shop.company, "%%"))
    assert nothing(search(shop.company, "__"))


def test_page_lists_results_and_needs_login(client, shop):
    assert client.get("/suche/", {"q": "kunde"}).status_code == 302
    client.force_login(shop.user)
    page = client.get("/suche/", {"q": "kunde"}).content.decode()
    assert f"/kunden/{shop.org.pk}/" in page and "Kontakte" in page
    assert "Nichts gefunden" in client.get("/suche/", {"q": "zzzz"}).content.decode()


@pytest.mark.parametrize(
    "query",
    ["%", "' OR 1=1 --", "<script>alert(1)</script>", "\x00abc", "ä" * 200, "\\", "{{ 7*7 }}", "fett"],
)
def test_garbage_queries_never_crash_or_inject(client, shop, query):
    create_customer(shop.company, name="<b>fett</b>", street="W", postcode="1", city="K")
    client.force_login(shop.user)
    response = client.get("/suche/", {"q": query})
    assert response.status_code == 200
    page = response.content.decode()
    assert "<script>alert(1)</script>" not in page and "<b>fett</b>" not in page
