"""Stammdaten: Leistungskatalog und Kunden (Phase 1a, plan §6.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from skontio.catalog.models import Item, Unit
from skontio.catalog.services import archive_item, create_item, search_items
from skontio.core.models import Company
from skontio.crm.models import Customer
from skontio.crm.services import create_customer, search_customers
from skontio.tax.engine import TaxTreatment


@pytest.fixture
def company(db):
    return Company.objects.create(name="Muster")


@pytest.fixture
def other_company(db):
    return Company.objects.create(name="Andere")


@pytest.mark.django_db
class TestUnits:
    def test_common_units_are_seeded(self):
        codes = set(Unit.objects.values_list("code", flat=True))
        assert {"C62", "HUR", "DAY", "MON", "LS", "KMT"} <= codes
        assert Unit.objects.get(code="HUR").label_de == "Stunde"


@pytest.mark.django_db
class TestCatalog:
    def test_create_item_with_abstract_tax_treatment(self, company):
        item = create_item(company, name="Beratung", unit="HUR", net_price=Decimal("80"))
        assert item.tax_treatment == TaxTreatment.STANDARD
        assert item.kind == Item.Kind.SERVICE

    def test_search_finds_by_name_sku_and_description(self, company):
        create_item(company, name="Beratung", unit="HUR", net_price=Decimal("80"), sku="B-1")
        create_item(company, name="Workshop", unit="DAY", net_price=Decimal("900"), description="inkl. Beratung")
        create_item(company, name="Hosting", unit="MON", net_price=Decimal("10"))
        assert {i.name for i in search_items(company, "beratung")} == {"Beratung", "Workshop"}
        assert [i.name for i in search_items(company, "B-1")] == ["Beratung"]

    def test_search_is_scoped_to_company(self, company, other_company):
        create_item(other_company, name="Beratung", unit="HUR", net_price=Decimal("80"))
        assert list(search_items(company, "Beratung")) == []

    def test_archived_items_are_hidden_from_search(self, company):
        item = create_item(company, name="Altleistung", unit="C62", net_price=Decimal("1"))
        archive_item(item)
        assert list(search_items(company, "Alt")) == []
        assert list(search_items(company, "Alt", include_archived=True)) == [item]

    def test_sku_is_unique_per_company(self, company, other_company):
        create_item(company, name="A", unit="C62", net_price=Decimal("1"), sku="X")
        create_item(other_company, name="A", unit="C62", net_price=Decimal("1"), sku="X")
        with pytest.raises(ValueError):
            create_item(company, name="B", unit="C62", net_price=Decimal("1"), sku="X")

    def test_negative_price_is_rejected(self, company):
        with pytest.raises(ValueError):
            create_item(company, name="A", unit="C62", net_price=Decimal("-1"))

    def test_unknown_unit_is_rejected(self, company):
        with pytest.raises(ValueError):
            create_item(company, name="A", unit="XYZ", net_price=Decimal("1"))


@pytest.mark.django_db
class TestCustomers:
    def test_customer_numbers_are_assigned_sequentially(self, company):
        first = create_customer(company, name="Kunde GmbH", kind=Customer.Kind.ORGANISATION)
        second = create_customer(company, name="Erika Muster", kind=Customer.Kind.PERSON)
        assert (first.number, second.number) == ("K-00001", "K-00002")

    def test_numbers_are_per_company(self, company, other_company):
        create_customer(company, name="A")
        assert create_customer(other_company, name="B").number == "K-00001"

    def test_defaults(self, company):
        customer = create_customer(company, name="Kunde GmbH")
        assert customer.country == "DE"
        assert customer.language == "de"
        assert customer.payment_terms_days == 14

    def test_search_by_name_number_city_and_email(self, company):
        create_customer(company, name="Kunde GmbH", city="Köln", email="einkauf@kunde.example")
        create_customer(company, name="Erika Muster", city="Bonn")
        assert [c.name for c in search_customers(company, "kunde")] == ["Kunde GmbH"]
        assert [c.name for c in search_customers(company, "K-00002")] == ["Erika Muster"]
        assert [c.name for c in search_customers(company, "bonn")] == ["Erika Muster"]
        assert [c.name for c in search_customers(company, "einkauf@")] == ["Kunde GmbH"]

    def test_search_is_scoped_to_company(self, company, other_company):
        create_customer(other_company, name="Fremdkunde")
        assert list(search_customers(company, "Fremd")) == []

    def test_tags(self, company):
        customer = create_customer(company, name="Kunde GmbH", tags=["Stammkunde", "B2B"])
        assert sorted(customer.tags.values_list("name", flat=True)) == ["B2B", "Stammkunde"]
        again = create_customer(company, name="Zweite GmbH", tags=["B2B"])
        assert again.tags.get().pk == customer.tags.get(name="B2B").pk

    def test_vat_id_format_is_checked(self, company):
        with pytest.raises(ValueError):
            create_customer(company, name="X", vat_id="DE12")
        assert create_customer(company, name="Y", vat_id="de 123 456 789").vat_id == "DE123456789"
