"""MCP-Alltagswerkzeuge: Kunden ändern, Entwürfe überblicken und verwerfen, offene Bankumsätze – mit Grenzen."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.audit.models import AuditEvent
from skontio.banking.models import BankTransaction
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.tools.registry import ToolError, call_tool

DAY = dt.date(2026, 3, 2)


def test_customer_update_changes_only_given_fields(ctx, shop):
    before = shop.org.city
    args = {"customer_number": shop.org.number, "street": "Neue Str. 5", "vat_id": "de 123 456 789"}
    assert call_tool(ctx, "customer_update", args)
    shop.org.refresh_from_db()
    assert shop.org.street == "Neue Str. 5" and shop.org.city == before and shop.org.vat_id == "DE123456789"
    assert AuditEvent.objects.filter(action="customer.update").exists()
    with pytest.raises(ToolError):
        call_tool(ctx, "customer_update", {"customer_number": shop.org.number, "vat_id": "12"})
    with pytest.raises(ToolError):
        call_tool(ctx, "customer_update", {"customer_number": "K-99999", "city": "X"})


def test_draft_list_and_discard_only_unreviewed_ai_drafts(ctx, shop):
    ai = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=DAY, origin=Document.Origin.MCP)
    ui = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=DAY)
    listed = {d["document_id"] for d in call_tool(ctx, "draft_list", {})["drafts"]}
    assert {ai.pk, ui.pk} <= listed
    with pytest.raises(ToolError):
        call_tool(ctx, "draft_discard", {"document_id": ui.pk})  # made by a person: stays
    call_tool(ctx, "draft_discard", {"document_id": ai.pk})
    assert not Document.objects.filter(pk=ai.pk).exists() and Document.objects.filter(pk=ui.pk).exists()
    add_line_from_item(ui, shop.item, quantity=Decimal("1"))
    final = finalize(ui)
    with pytest.raises(ToolError):
        call_tool(ctx, "draft_discard", {"document_id": final.pk})


def test_bank_open_lists_suggestions_without_iban(ctx, shop):
    doc = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=DAY, service_date=DAY)
    add_line_from_item(doc, shop.item, quantity=Decimal("1"))
    invoice = finalize(doc)
    BankTransaction.objects.create(
        company=shop.company, booked_on=dt.date(2026, 3, 9), amount=invoice.gross_total,
        purpose=f"Rechnung {invoice.number}", iban="DE89370400440532013000", counterparty="Kunde GmbH",
        import_key="x", source="csv",
    )  # fmt: skip
    rows = call_tool(ctx, "bank_open", {})["transactions"]
    assert rows[0]["suggestions"][0]["invoice"] == invoice.number
    assert "DE89370400440532013000" not in str(rows)
