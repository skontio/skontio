"""Zahlungsziel: Firmenstandard, Abweichung je Kunde, Grenzen."""

from __future__ import annotations

import datetime as dt

import pytest

from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import create_draft

ISSUE = dt.date(2026, 3, 2)


@pytest.fixture
def company(shop):
    shop.company.default_payment_terms_days = 30
    shop.company.save()
    return shop.company


class TestDueDate:
    def test_customer_without_own_terms_uses_the_company_default(self, company):
        customer = create_customer(company, name="Standard GmbH")
        draft = create_draft(company, Document.Type.INVOICE, customer, issue_date=ISSUE)
        assert draft.due_date == ISSUE + dt.timedelta(days=30)

    def test_own_terms_win_even_when_zero(self, company):
        customer = create_customer(company, name="Sofort GmbH", payment_terms_days=0)
        draft = create_draft(company, Document.Type.INVOICE, customer, issue_date=ISSUE)
        assert draft.due_date == ISSUE

    def test_changing_the_default_does_not_touch_existing_drafts(self, company):
        customer = create_customer(company, name="Standard GmbH")
        draft = create_draft(company, Document.Type.INVOICE, customer, issue_date=ISSUE)
        company.default_payment_terms_days = 7
        company.save()
        draft.refresh_from_db()
        assert draft.due_date == ISSUE + dt.timedelta(days=30)

    def test_quotes_get_no_due_date(self, company):
        customer = create_customer(company, name="Standard GmbH")
        assert create_draft(company, Document.Type.QUOTE, customer, issue_date=ISSUE).due_date is None


class TestForms:
    def test_blank_terms_mean_company_default(self, client, shop):
        client.force_login(shop.user)
        client.post(
            f"/kunden/{shop.org.pk}/",
            {"kind": "org", "name": shop.org.name, "street": "W 1", "postcode": "2", "city": "K", "country": "DE",
             "language": "de", "payment_terms_days": ""},
        )  # fmt: skip
        shop.org.refresh_from_db()
        assert shop.org.payment_terms_days is None

    @pytest.mark.parametrize("value", ["-1", "366", "NaN", "1e3", "14.5"])
    def test_absurd_terms_are_refused(self, client, shop, value):
        client.force_login(shop.user)
        before = shop.org.payment_terms_days
        response = client.post(
            f"/kunden/{shop.org.pk}/",
            {"kind": "org", "name": shop.org.name, "country": "DE", "language": "de", "payment_terms_days": value},
        )
        assert response.status_code == 200  # form shown again with an error
        shop.org.refresh_from_db()
        assert shop.org.payment_terms_days == before

    def test_ai_tool_leaves_terms_on_default_unless_given(self, ctx):
        from skontio.crm.models import Customer
        from skontio.tools.registry import call_tool

        call_tool(ctx, "customer_create", {"name": "KI Kunde GmbH"})
        assert Customer.objects.get(name="KI Kunde GmbH").payment_terms_days is None
