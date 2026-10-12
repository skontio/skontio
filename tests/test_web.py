"""Oberfläche: Login-Pflicht, Einrichtung, Dashboard, Kunden und Leistungen."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.catalog.models import Item
from skontio.core.models import Company
from skontio.crm.models import Customer
from skontio.tax.engine import Regime
from skontio.taxstatus.models import TaxStatusPeriod
from skontio.taxstatus.services import start_regime


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user("inhaber", is_staff=True, is_superuser=True)


@pytest.fixture
def member(django_user_model):
    return django_user_model.objects.create_user("mitarbeit")


@pytest.fixture
def company(db):
    company = Company.objects.create(
        name="Muster Selbstständig",
        street="Musterweg 1",
        postcode="12345",
        city="Musterstadt",
        tax_number="123/456/78901",
        iban="DE02120300000000202051",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def app(client, owner, company):
    client.force_login(owner)
    return client


SETUP = {
    "name": "Neue Firma",
    "street": "Weg 1",
    "postcode": "12345",
    "city": "Stadt",
    "country": "DE",
    "activity": "trade",
    "side_business": "on",
    "tax_number": "123/456/78901",
    "iban": "DE02 1203 0000 0000 2020 51",
    "regime": "small_business",
    "regime_since": "2024-01-01",
}


@pytest.mark.django_db
class TestAccess:
    @pytest.mark.parametrize("url", ["/", "/kunden/", "/leistungen/", "/einrichtung/"])
    def test_login_required(self, client, url):
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"].startswith("/auth/login")

    def test_without_company_owner_is_sent_to_setup(self, client, owner):
        client.force_login(owner)
        response = client.get("/")
        assert response.status_code == 302 and response["Location"] == "/einrichtung/"

    def test_without_company_member_sees_hint(self, client, member):
        client.force_login(member)
        assert client.get("/").status_code == 503

    def test_setup_is_for_admins_only(self, client, member, company):
        client.force_login(member)
        assert client.get("/einrichtung/").status_code == 403

    def test_security_headers(self, app):
        response = app.get("/")
        assert response["X-Frame-Options"] == "DENY"
        assert "default-src 'self'" in response["Content-Security-Policy"]


@pytest.mark.django_db
class TestSetup:
    def test_setup_creates_company_and_initial_tax_status(self, client, owner):
        client.force_login(owner)
        response = client.post("/einrichtung/", SETUP)
        assert response.status_code == 302
        company = Company.objects.get()
        assert company.side_business is True
        assert company.iban == "DE02120300000000202051"
        assert TaxStatusPeriod.objects.get(company=company).regime == Regime.SMALL_BUSINESS

    def test_invalid_iban_is_rejected(self, client, owner):
        client.force_login(owner)
        response = client.post("/einrichtung/", SETUP | {"iban": "DE00 1234"})
        assert response.status_code == 200
        assert "IBAN" in response.content.decode()
        assert not Company.objects.exists()

    def test_setup_edits_existing_company_without_touching_tax_status(self, app, company):
        response = app.get("/einrichtung/")
        assert response.status_code == 200
        assert "regime" not in response.context["form"].fields  # changes only via the regime-change workflow


@pytest.mark.django_db
class TestDashboard:
    def test_shows_small_business_status_and_disclaimer(self, app):
        body = app.get("/").content.decode()
        assert "Kleinunternehmer" in body
        assert "100.000" in body
        assert "keine Steuerberatung" in body


@pytest.mark.django_db
class TestCustomers:
    def test_create_and_list(self, app, company):
        response = app.post(
            "/kunden/neu/",
            {
                "kind": "org",
                "name": "Kunde GmbH",
                "street": "Weg 5",
                "postcode": "54321",
                "city": "Köln",
                "country": "DE",
                "language": "de",
                "payment_terms_days": "14",
            },
        )
        customer = Customer.objects.get()
        assert response.status_code == 302 and response["Location"] == f"/kunden/{customer.pk}/"
        assert customer.number == "K-00001"
        assert "Kunde GmbH" in app.get("/kunden/").content.decode()

    def test_live_search_returns_partial(self, app, company):
        Customer.objects.create(company=company, number="K-1", name="Alpha AG")
        Customer.objects.create(company=company, number="K-2", name="Beta GmbH")
        response = app.get("/kunden/", {"q": "alpha"}, HTTP_HX_REQUEST="true")
        body = response.content.decode()
        assert "Alpha AG" in body and "Beta GmbH" not in body
        assert "<html" not in body  # HTMX gets the fragment only

    def test_edit_keeps_number(self, app, company):
        customer = Customer.objects.create(company=company, number="K-7", name="Alt")
        app.post(
            f"/kunden/{customer.pk}/",
            {"kind": "org", "name": "Neu", "country": "DE", "language": "de", "payment_terms_days": "30"},
        )
        customer.refresh_from_db()
        assert (customer.name, customer.number, customer.payment_terms_days) == ("Neu", "K-7", 30)

    def test_customers_of_other_companies_are_not_reachable(self, app, company):
        other = Company.objects.create(name="Fremd")
        foreign = Customer.objects.create(company=other, number="K-1", name="Fremdkunde")
        assert app.get(f"/kunden/{foreign.pk}/").status_code == 404


@pytest.mark.django_db
class TestCatalog:
    def test_create_item_with_german_decimal(self, app, company):
        response = app.post(
            "/leistungen/neu/",
            {"kind": "service", "name": "Beratung", "unit": "HUR", "net_price": "80,00", "tax_treatment": "standard"},
        )
        assert response.status_code == 302
        item = Item.objects.get()
        assert (item.name, item.net_price, item.unit.code) == ("Beratung", Decimal("80.0000"), "HUR")

    def test_archive_hides_item(self, app, company):
        app.post(
            "/leistungen/neu/",
            {"kind": "service", "name": "Alt", "unit": "C62", "net_price": "1", "tax_treatment": "standard"},
        )
        item = Item.objects.get()
        assert app.post(f"/leistungen/{item.pk}/archivieren/").status_code == 302
        assert "Alt" not in app.get("/leistungen/").content.decode()
