"""Lieferanten als Kontakte: eigene Nummern, Ausgaben verknüpfen, nie als Rechnungsempfänger."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.core.models import Company
from skontio.crm.models import Customer
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import DocumentError, create_draft
from skontio.expenses.models import Expense
from skontio.expenses.services import ExpenseError, create_expense
from skontio.tools.registry import ToolError, call_tool

TODAY = dt.date.today()


@pytest.fixture
def supplier(shop):
    return create_customer(shop.company, name="Hosting GmbH", role=Customer.Role.SUPPLIER, country="DE")


class TestContacts:
    def test_suppliers_get_their_own_number_range(self, shop, supplier):
        assert supplier.number == "L-00001"
        assert create_customer(shop.company, name="Beides AG", role=Customer.Role.BOTH).number.startswith("K-")

    def test_unknown_role_is_refused(self, shop):
        with pytest.raises(ValueError, match="Rolle"):
            create_customer(shop.company, name="X", role="admin")

    def test_list_filters_by_role(self, client, shop, supplier):
        client.force_login(shop.user)
        assert "Hosting GmbH" in client.get("/kunden/?rolle=lieferanten").content.decode()
        page = client.get("/kunden/?rolle=kunden").content.decode()
        assert "Hosting GmbH" not in page and shop.org.name in page
        assert client.get("/kunden/?rolle=<script>").status_code == 200  # unknown filter: all contacts

    def test_new_supplier_form_preselects_the_role(self, client, shop):
        client.force_login(shop.user)
        assert 'value="supplier" selected' in client.get("/kunden/neu/?rolle=lieferant").content.decode()


class TestNeverAnInvoiceRecipient:
    def test_service_refuses_a_supplier_only_contact(self, shop, supplier):
        with pytest.raises(DocumentError, match="nur als Lieferant"):
            create_draft(shop.company, Document.Type.INVOICE, supplier, issue_date=TODAY)

    def test_contact_with_both_roles_may_be_invoiced(self, shop):
        both = create_customer(
            shop.company, name="Beides AG", role=Customer.Role.BOTH, street="W", postcode="1", city="K"
        )
        assert create_draft(shop.company, Document.Type.INVOICE, both, issue_date=TODAY).customer == both

    def test_new_document_form_does_not_offer_suppliers_and_refuses_a_forged_post(self, client, shop, supplier):
        client.force_login(shop.user)
        assert "Hosting GmbH" not in client.get("/dokumente/neu/").content.decode()
        client.post(
            "/dokumente/neu/", {"doc_type": "invoice", "customer": supplier.pk, "issue_date": TODAY.isoformat()}
        )
        assert not Document.objects.filter(customer=supplier).exists()

    def test_ai_tool_cannot_invoice_a_supplier(self, ctx, supplier):
        line = {"item_id": None, "text": "X", "quantity": "1", "unit": "C62", "unit_price": "1"}
        args = {
            "customer_number": supplier.number, "idempotency_key": "lief-0001-abcdef",
            "issue_date": TODAY.isoformat(), "service_date": TODAY.isoformat(), "lines": [line],
        }  # fmt: skip
        with pytest.raises(ToolError):
            call_tool(ctx, "invoice_draft_create", args)


class TestExpenses:
    def test_expense_takes_name_and_country_from_the_contact(self, shop):
        irish = create_customer(shop.company, name="Ads Ltd", role=Customer.Role.SUPPLIER, country="IE")
        expense = create_expense(
            shop.company, receipt_date=TODAY, supplier="", supplier_country="", supplier_contact=irish,
            gross_amount=Decimal("50"), vat_rate=Decimal("0"),
        )  # fmt: skip
        assert (expense.supplier, expense.supplier_country) == ("Ads Ltd", "IE")

    def test_receipt_name_is_kept_when_given(self, shop, supplier):
        expense = create_expense(
            shop.company, receipt_date=TODAY, supplier="Hosting GmbH & Co. KG", supplier_contact=supplier,
            gross_amount=Decimal("10"), vat_rate=Decimal("19"),
        )  # fmt: skip
        assert expense.supplier == "Hosting GmbH & Co. KG"

    def test_customer_only_contact_is_refused_as_supplier(self, shop):
        with pytest.raises(ExpenseError, match="kein Lieferant"):
            create_expense(
                shop.company, receipt_date=TODAY, supplier_contact=shop.org, gross_amount=Decimal("1"),
                vat_rate=Decimal("19"),
            )  # fmt: skip

    def test_contact_of_another_company_is_refused(self, shop):
        other = Company.objects.create(name="Fremd", tax_number="9/9/9")
        foreign = create_customer(other, name="Fremd-Lieferant", role=Customer.Role.SUPPLIER)
        with pytest.raises(ExpenseError):
            create_expense(
                shop.company, receipt_date=TODAY, supplier_contact=foreign, gross_amount=Decimal("1"),
                vat_rate=Decimal("19"),
            )  # fmt: skip

    def test_form_with_contact_only_and_forged_contact(self, client, shop, supplier):
        client.force_login(shop.user)
        base = {
            "receipt_date": TODAY.isoformat(), "gross_amount": "11,90", "vat_rate": "19", "category": "software",
            "method": "bank", "supplier_country": "DE",
        }  # fmt: skip
        client.post("/ausgaben/neu/", {**base, "supplier_contact": supplier.pk})
        assert Expense.objects.get().supplier == "Hosting GmbH"
        response = client.post("/ausgaben/neu/", {**base, "supplier_contact": shop.org.pk})  # a customer
        assert response.status_code == 200 and Expense.objects.count() == 1
        response = client.post("/ausgaben/neu/", base)  # neither text nor contact
        assert "Lieferanten angeben" in response.content.decode() and Expense.objects.count() == 1

    def test_supplier_page_lists_its_expenses(self, client, shop, supplier):
        create_expense(
            shop.company, receipt_date=TODAY, supplier="", supplier_contact=supplier, gross_amount=Decimal("10"),
            vat_rate=Decimal("19"), description="Server Oktober",
        )  # fmt: skip
        client.force_login(shop.user)
        page = client.get(f"/kunden/{supplier.pk}/").content.decode()
        assert "Server Oktober" in page and "Erste Rechnung schreiben" not in page
