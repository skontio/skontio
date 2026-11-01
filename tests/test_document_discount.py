"""Rabatt auf die Gesamtsumme: je Steuerkategorie in der E-Rechnung (BG-20), Summen stimmen, Grenzen."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from lxml import etree

from skontio.archive.services import load
from skontio.documents.models import Document, Rendition
from skontio.documents.services import DocumentError, add_line, create_draft, finalize, update_draft
from skontio.einvoice.cii import Profile, build_cii
from skontio.einvoice.mapping import invoice_from_document
from skontio.einvoice.reader import parse_einvoice
from skontio.tax.engine import TaxTreatment

DAY = dt.date(2026, 3, 2)
RAM = "{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}"


@pytest.fixture
def discounted(shop):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=DAY, service_date=DAY)
    add_line(draft, text="Beratung", quantity=Decimal("1"), unit_code="C62", unit_price=Decimal("100"))
    add_line(
        draft, text="Buch", quantity=Decimal("1"), unit_code="C62", unit_price=Decimal("50"),
        tax_treatment=TaxTreatment.REDUCED,
    )  # fmt: skip
    update_draft(draft, document_discount_percent=Decimal("10"))
    return finalize(draft)


def test_totals_follow_the_discount(discounted):
    assert discounted.net_total == Decimal("135.00")  # 150 − 15
    assert discounted.tax_total == Decimal("17.10") + Decimal("3.15")
    assert discounted.gross_total == Decimal("155.25")


def test_e_invoice_has_one_document_allowance_per_vat_category(discounted):
    root = etree.fromstring(build_cii(invoice_from_document(discounted), Profile.EN16931))
    settlement = root.find(f".//{RAM}ApplicableHeaderTradeSettlement")
    allowances = settlement.findall(f"{RAM}SpecifiedTradeAllowanceCharge")
    assert sorted(Decimal(a.findtext(f"{RAM}ActualAmount")) for a in allowances) == [Decimal("5.00"), Decimal("10.00")]
    summation = settlement.find(f"{RAM}SpecifiedTradeSettlementHeaderMonetarySummation")
    assert Decimal(summation.findtext(f"{RAM}AllowanceTotalAmount")) == Decimal("15.00")
    assert Decimal(summation.findtext(f"{RAM}TaxBasisTotalAmount")) == Decimal("135.00")


def test_archived_zugferd_reads_back_the_same_totals(discounted):
    pdf = load(Rendition.objects.get(document=discounted, kind=Rendition.Kind.ZUGFERD_PDF).blob)
    parsed = parse_einvoice(pdf)
    assert (parsed.net_total, parsed.tax_total, parsed.gross_total) == (
        Decimal("135.00"), Decimal("20.25"), Decimal("155.25"),
    )  # fmt: skip


@pytest.mark.parametrize("value", ["-1", "100.01", "250", "NaN"])
def test_discount_outside_0_to_100_percent_is_refused(shop, value):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=DAY, service_date=DAY)
    with pytest.raises((DocumentError, ValueError, ArithmeticError)):
        update_draft(draft, document_discount_percent=Decimal(value))
    draft.refresh_from_db()
    assert draft.document_discount_percent == 0


def test_full_discount_gives_a_zero_invoice_not_a_negative_one(shop):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=DAY, service_date=DAY)
    add_line(draft, text="Kulanz", quantity=Decimal("1"), unit_code="C62", unit_price=Decimal("80"))
    update_draft(draft, document_discount_percent=Decimal("100"))
    invoice = finalize(draft)
    assert invoice.gross_total == Decimal("0.00") and invoice.net_total == Decimal("0.00")
