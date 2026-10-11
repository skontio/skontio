"""Phase-0 spike: EN 16931 output for a Kleinunternehmer, checked by the official validators.

Unit tests run everywhere. Tests marked ``einvoice`` run the KoSIT validator (XRechnung)
and Mustang (ZUGFeRD PDF/A-3, bundles veraPDF); they are skipped when
``tools/validators/fetch.sh`` has not been run or Java is missing.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import facturx
import pytest
from lxml import etree

from skontio.einvoice.cii import EInvoiceDataError, Profile, build_cii
from skontio.einvoice.hybrid import embed_xml
from skontio.einvoice.model import Invoice, InvoiceLine, Party, PaymentInstructions
from skontio.einvoice.validation import ValidatorUnavailable, validate_with_kosit, validate_with_mustang
from skontio.rendering.pdf import render_pdf_a3

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
}


@pytest.fixture
def small_business_invoice() -> Invoice:
    seller = Party(
        name="Muster Selbstständig",
        street="Musterweg 1",
        postcode="12345",
        city="Musterstadt",
        country="DE",
        tax_number="123/456/78901",
        identifier="123/456/78901",  # BT-29, needed for BR-CO-26 without USt-IdNr.
        email="rechnung@example.org",
        phone="+49 30 1234567",
        contact_name="Max Muster",
    )
    buyer = Party(
        name="Kunde GmbH",
        street="Kundenstraße 5",
        postcode="54321",
        city="Kundenstadt",
        country="DE",
        email="einkauf@kunde.example",
    )
    return Invoice(
        number="RE-2026-0001",
        issue_date=dt.date(2026, 10, 11),
        service_date=dt.date(2026, 10, 9),
        due_date=dt.date(2026, 10, 25),
        seller=seller,
        buyer=buyer,
        buyer_reference="Bestellung 4711",
        lines=[
            InvoiceLine(name="Beratung", quantity=Decimal("3"), unit_code="HUR", unit_price=Decimal("80.00")),
            InvoiceLine(name="Workshop", quantity=Decimal("1"), unit_code="C62", unit_price=Decimal("249.99")),
        ],
        tax_category="E",
        tax_rate=Decimal("0"),
        tax_exemption_reason="Gemäß § 19 UStG wird keine Umsatzsteuer berechnet (Kleinunternehmerregelung).",
        payment=PaymentInstructions(iban="DE02120300000000202051", bic="BYLADEM1001", account_name="Max Muster"),
        payment_terms="Zahlbar ohne Abzug bis 25.10.2026.",
    )


def xpath(xml: bytes, path: str) -> list[str]:
    return [str(v) for v in etree.fromstring(xml).xpath(path, namespaces=NS)]


class TestCii:
    def test_totals_and_small_business_tax(self, small_business_invoice):
        xml = build_cii(small_business_invoice, Profile.XRECHNUNG)
        summation = "//ram:SpecifiedTradeSettlementHeaderMonetarySummation"
        assert xpath(xml, f"{summation}/ram:LineTotalAmount/text()") == ["489.99"]
        assert xpath(xml, f"{summation}/ram:TaxTotalAmount/text()") == ["0.00"]
        assert xpath(xml, f"{summation}/ram:GrandTotalAmount/text()") == ["489.99"]
        tax = "//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax"
        assert xpath(xml, f"{tax}/ram:CategoryCode/text()") == ["E"]
        assert "§ 19 UStG" in xpath(xml, f"{tax}/ram:ExemptionReason/text()")[0]

    def test_guideline_ids(self, small_business_invoice):
        guideline = "//rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID/text()"
        assert xpath(build_cii(small_business_invoice, Profile.XRECHNUNG), guideline) == [
            "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
        ]
        assert xpath(build_cii(small_business_invoice, Profile.EN16931), guideline) == ["urn:cen.eu:en16931:2017"]

    def test_service_date_is_present(self, small_business_invoice):
        # § 14 Abs. 4 Nr. 6 UStG: Zeitpunkt der Leistung
        xml = build_cii(small_business_invoice, Profile.EN16931)
        assert xpath(xml, "//ram:ActualDeliverySupplyChainEvent//*[local-name()='DateTimeString']/text()") == [
            "20261009"
        ]

    def test_seller_tax_number_uses_scheme_fc(self, small_business_invoice):
        xml = build_cii(small_business_invoice, Profile.EN16931)
        assert xpath(xml, "//ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID/@schemeID") == ["FC"]

    def test_buyer_tax_number_is_never_emitted(self, small_business_invoice):
        # FX-SCH-A-000570: the buyer may only carry a VAT identifier (BT-48, scheme VA)
        buyer = dataclasses.replace(small_business_invoice.buyer, tax_number="99/999/99999")
        xml = build_cii(dataclasses.replace(small_business_invoice, buyer=buyer), Profile.EN16931)
        assert xpath(xml, "//ram:BuyerTradeParty/ram:SpecifiedTaxRegistration/ram:ID/@schemeID") == []

    def test_service_date_is_mandatory(self, small_business_invoice):
        with pytest.raises(EInvoiceDataError, match="Leistungsdatum"):
            build_cii(dataclasses.replace(small_business_invoice, service_date=None), Profile.EN16931)

    def test_seller_without_vat_id_or_identifier_is_rejected(self, small_business_invoice):
        # BR-CO-26: BT-29, BT-30 or BT-31 – the Steuernummer (BT-32) alone is not enough
        seller = dataclasses.replace(small_business_invoice.seller, identifier=None, vat_id=None)
        invoice = dataclasses.replace(small_business_invoice, seller=seller)
        with pytest.raises(EInvoiceDataError, match="BR-CO-26"):
            build_cii(invoice, Profile.EN16931)


class TestHybridPdf:
    def test_embedded_xml_round_trips(self, small_business_invoice):
        xml = build_cii(small_business_invoice, Profile.EN16931)
        pdf = embed_xml(render_pdf_a3("<h1>Rechnung RE-2026-0001</h1>", title="Rechnung RE-2026-0001"), xml)
        filename, extracted = facturx.get_xml_from_pdf(pdf, check_xsd=False)
        assert filename == "factur-x.xml"
        assert extracted == xml
        assert facturx.get_level(etree.fromstring(extracted)) == "en16931"


@pytest.mark.einvoice
class TestOfficialValidators:
    def test_xrechnung_is_accepted_by_kosit(self, small_business_invoice, tmp_path):
        try:
            result = validate_with_kosit(build_cii(small_business_invoice, Profile.XRECHNUNG), workdir=tmp_path)
        except ValidatorUnavailable as exc:
            pytest.skip(str(exc))
        assert result.valid, result.summary

    def test_zugferd_pdf_is_valid_for_mustang(self, small_business_invoice, tmp_path):
        xml = build_cii(small_business_invoice, Profile.EN16931)
        pdf = embed_xml(render_pdf_a3("<h1>Rechnung RE-2026-0001</h1>", title="Rechnung RE-2026-0001"), xml)
        try:
            result = validate_with_mustang(pdf, workdir=tmp_path)
        except ValidatorUnavailable as exc:
            pytest.skip(str(exc))
        assert result.valid, result.summary
        assert b"flavour=3b" in result.report and b"isCompliant=true" in result.report  # veraPDF PDF/A-3b
