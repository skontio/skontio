"""EN 16931 output (XRechnung, ZUGFeRD) checked by the official validators.

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
from skontio.einvoice.model import LineData, Party, PaymentInstructions, PrecedingInvoice, build_invoice
from skontio.einvoice.validation import ValidatorUnavailable, validate_with_kosit, validate_with_mustang
from skontio.rendering.pdf import render_pdf_a3
from skontio.tax.engine import Regime, TaxTreatment, resolve

D = Decimal
NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
}
SERVICE_DATE = dt.date(2026, 10, 9)

SELLER = Party(
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
BUYER = Party(
    name="Kunde GmbH",
    street="Kundenstraße 5",
    postcode="54321",
    city="Kundenstadt",
    country="DE",
    email="einkauf@kunde.example",
)
PAYMENT = PaymentInstructions(iban="DE02120300000000202051", bic="BYLADEM1001", account_name="Max Muster")


def tax(treatment: TaxTreatment, regime: Regime = Regime.STANDARD):
    return resolve(treatment, regime, SERVICE_DATE)


def make(lines, *, seller=SELLER, buyer=BUYER, **overrides):
    defaults = dict(
        number="RE-2026-0001",
        issue_date=dt.date(2026, 10, 11),
        service_date=SERVICE_DATE,
        due_date=dt.date(2026, 10, 25),
        seller=seller,
        buyer=buyer,
        buyer_reference="Bestellung 4711",
        payment=PAYMENT,
        payment_terms="Zahlbar ohne Abzug bis 25.10.2026.",
        lines=lines,
    )
    return build_invoice(**{**defaults, **overrides})


@pytest.fixture
def small_business_invoice():
    ku = tax(TaxTreatment.STANDARD, Regime.SMALL_BUSINESS)
    return make(
        [
            LineData("Beratung", D("3"), "HUR", D("80.00"), ku),
            LineData("Workshop", D("1"), "C62", D("249.99"), ku),
        ]
    )


@pytest.fixture
def mixed_rate_invoice():
    seller = dataclasses.replace(SELLER, vat_id="DE123456789", identifier=None)
    return make(
        [
            LineData("Beratung", D("3"), "HUR", D("80.00"), tax(TaxTreatment.STANDARD), discount_percent=D("10")),
            LineData("Fachbuch", D("2"), "C62", D("39.90"), tax(TaxTreatment.REDUCED)),
        ],
        seller=seller,
        document_discount_percent=D("5"),
    )


@pytest.fixture
def reverse_charge_invoice():
    seller = dataclasses.replace(SELLER, vat_id="DE123456789", identifier=None)
    buyer = Party(
        name="Client SARL",
        street="1 Rue de Paris",
        postcode="75001",
        city="Paris",
        country="FR",
        email="compta@client.example",
        vat_id="FR40303265045",
    )
    return make(
        [LineData("Beratung", D("10"), "HUR", D("90.00"), tax(TaxTreatment.REVERSE_CHARGE))], seller=seller, buyer=buyer
    )


@pytest.fixture
def cancellation_invoice(small_business_invoice):
    ku = tax(TaxTreatment.STANDARD, Regime.SMALL_BUSINESS)
    return make(
        [LineData("Beratung", D("3"), "HUR", D("80.00"), ku)],
        number="ST-2026-0001",
        type_code="381",
        preceding_invoice=PrecedingInvoice("RE-2026-0001", dt.date(2026, 10, 11)),
        notes=["Stornierung der Rechnung RE-2026-0001: doppelt gestellt"],
    )


def xpath(xml: bytes, path: str) -> list[str]:
    return [str(v) for v in etree.fromstring(xml).xpath(path, namespaces=NS)]


SUMMATION = "//ram:SpecifiedTradeSettlementHeaderMonetarySummation"
HEADER_TAX = "//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax"


class TestCii:
    def test_totals_and_small_business_tax(self, small_business_invoice):
        xml = build_cii(small_business_invoice, Profile.XRECHNUNG)
        assert xpath(xml, f"{SUMMATION}/ram:LineTotalAmount/text()") == ["489.99"]
        assert xpath(xml, f"{SUMMATION}/ram:TaxTotalAmount/text()") == ["0.00"]
        assert xpath(xml, f"{SUMMATION}/ram:GrandTotalAmount/text()") == ["489.99"]
        assert xpath(xml, f"{HEADER_TAX}/ram:CategoryCode/text()") == ["E"]
        assert "§ 19 UStG" in xpath(xml, f"{HEADER_TAX}/ram:ExemptionReason/text()")[0]

    def test_mixed_rates_with_discounts(self, mixed_rate_invoice):
        xml = build_cii(mixed_rate_invoice, Profile.EN16931)
        # 3×80 −10 % = 216.00; 2×39.90 = 79.80; doc discount 5 %: 10.80 + 3.99
        assert xpath(xml, f"{SUMMATION}/ram:LineTotalAmount/text()") == ["295.80"]
        assert xpath(xml, f"{SUMMATION}/ram:AllowanceTotalAmount/text()") == ["14.79"]
        assert xpath(xml, f"{SUMMATION}/ram:TaxBasisTotalAmount/text()") == ["281.01"]
        assert sorted(xpath(xml, f"{HEADER_TAX}/ram:RateApplicablePercent/text()")) == ["19.00", "7.00"]

    def test_guideline_ids(self, small_business_invoice):
        guideline = "//rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID/text()"
        assert xpath(build_cii(small_business_invoice, Profile.XRECHNUNG), guideline) == [
            "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
        ]
        assert xpath(build_cii(small_business_invoice, Profile.EN16931), guideline) == ["urn:cen.eu:en16931:2017"]

    def test_service_date_is_present(self, small_business_invoice):
        xml = build_cii(small_business_invoice, Profile.EN16931)
        assert xpath(xml, "//ram:ActualDeliverySupplyChainEvent//*[local-name()='DateTimeString']/text()") == [
            "20261009"
        ]

    def test_service_period_instead_of_date(self, small_business_invoice):
        invoice = dataclasses.replace(
            small_business_invoice, service_date=None, service_period=(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
        )
        xml = build_cii(invoice, Profile.EN16931)
        period = "//ram:ApplicableHeaderTradeSettlement/ram:BillingSpecifiedPeriod"
        assert xpath(xml, f"{period}/ram:StartDateTime/*/text()") == ["20260901"]
        assert xpath(xml, f"{period}/ram:EndDateTime/*/text()") == ["20260930"]

    def test_seller_tax_number_uses_scheme_fc(self, small_business_invoice):
        xml = build_cii(small_business_invoice, Profile.EN16931)
        assert xpath(xml, "//ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID/@schemeID") == ["FC"]

    def test_buyer_tax_number_is_never_emitted(self, small_business_invoice):
        # FX-SCH-A-000570: the buyer may only carry a VAT identifier (BT-48, scheme VA)
        buyer = dataclasses.replace(BUYER, tax_number="99/999/99999")
        xml = build_cii(dataclasses.replace(small_business_invoice, buyer=buyer), Profile.EN16931)
        assert xpath(xml, "//ram:BuyerTradeParty/ram:SpecifiedTaxRegistration/ram:ID/@schemeID") == []

    def test_service_date_is_mandatory(self, small_business_invoice):
        with pytest.raises(EInvoiceDataError, match="Leistungsdatum"):
            build_cii(dataclasses.replace(small_business_invoice, service_date=None), Profile.EN16931)

    def test_seller_without_vat_id_or_identifier_is_rejected(self, small_business_invoice):
        # BR-CO-26: BT-29, BT-30 or BT-31 – the Steuernummer (BT-32) alone is not enough
        seller = dataclasses.replace(SELLER, identifier=None, vat_id=None)
        with pytest.raises(EInvoiceDataError, match="BR-CO-26"):
            build_cii(dataclasses.replace(small_business_invoice, seller=seller), Profile.EN16931)

    def test_reverse_charge_requires_both_vat_ids(self, reverse_charge_invoice):
        buyer = dataclasses.replace(reverse_charge_invoice.buyer, vat_id=None)
        with pytest.raises(EInvoiceDataError, match="USt-IdNr"):
            build_cii(dataclasses.replace(reverse_charge_invoice, buyer=buyer), Profile.EN16931)

    def test_cancellation_references_original(self, cancellation_invoice):
        xml = build_cii(cancellation_invoice, Profile.EN16931)
        assert xpath(xml, "//rsm:ExchangedDocument/ram:TypeCode/text()") == ["381"]
        assert xpath(xml, "//ram:InvoiceReferencedDocument/ram:IssuerAssignedID/text()") == ["RE-2026-0001"]


class TestHybridPdf:
    def test_embedded_xml_round_trips(self, small_business_invoice):
        xml = build_cii(small_business_invoice, Profile.EN16931)
        pdf = embed_xml(render_pdf_a3("<h1>Rechnung RE-2026-0001</h1>", title="Rechnung RE-2026-0001"), xml)
        filename, extracted = facturx.get_xml_from_pdf(pdf, check_xsd=False)
        assert filename == "factur-x.xml"
        assert extracted == xml
        assert facturx.get_level(etree.fromstring(extracted)) == "en16931"


@pytest.mark.einvoice
@pytest.mark.parametrize(
    "fixture", ["small_business_invoice", "mixed_rate_invoice", "reverse_charge_invoice", "cancellation_invoice"]
)
class TestOfficialValidators:
    def test_xrechnung_is_accepted_by_kosit(self, fixture, request, tmp_path):
        invoice = request.getfixturevalue(fixture)
        try:
            result = validate_with_kosit(build_cii(invoice, Profile.XRECHNUNG), workdir=tmp_path)
        except ValidatorUnavailable as exc:
            pytest.skip(str(exc))
        assert result.valid, result.summary

    def test_zugferd_pdf_is_valid_for_mustang(self, fixture, request, tmp_path):
        invoice = request.getfixturevalue(fixture)
        xml = build_cii(invoice, Profile.EN16931)
        pdf = embed_xml(render_pdf_a3(f"<h1>{invoice.number}</h1>", title=invoice.number), xml)
        try:
            result = validate_with_mustang(pdf, workdir=tmp_path)
        except ValidatorUnavailable as exc:
            pytest.skip(str(exc))
        assert result.valid, result.summary
        assert b"flavour=3b" in result.report and b"isCompliant=true" in result.report  # veraPDF PDF/A-3b
