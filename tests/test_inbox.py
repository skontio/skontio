"""E-Rechnungs-Eingang: ZUGFeRD/XRechnung (CII, UBL) einlesen, anzeigen, revisionssicher ablegen."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from skontio.core.models import Company
from skontio.einvoice.cii import Profile, build_cii
from skontio.einvoice.hybrid import embed_xml
from skontio.einvoice.model import LineData, Party, PaymentInstructions, build_invoice
from skontio.einvoice.reader import EInvoiceParseError, parse_einvoice
from skontio.inbox.models import IncomingInvoice
from skontio.inbox.services import InboxError, receive
from skontio.rendering.pdf import render_pdf_a3
from skontio.tax.engine import Regime, TaxTreatment, resolve

D = Decimal

UBL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0</cbc:CustomizationID>
  <cbc:ID>HOST-2026-117</cbc:ID>
  <cbc:IssueDate>2026-10-01</cbc:IssueDate>
  <cbc:DueDate>2026-10-15</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyTaxScheme><cbc:CompanyID>DE811111111</cbc:CompanyID>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyLegalEntity><cbc:RegistrationName>Hosting AG</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyLegalEntity><cbc:RegistrationName>Muster Selbstständig</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode>
    <cac:PayeeFinancialAccount><cbc:ID>DE02120300000000202051</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">1.90</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">10.00</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">10.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">11.90</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">11.90</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine><cbc:ID>1</cbc:ID><cbc:InvoicedQuantity unitCode="MON">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">10.00</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>Webhosting Oktober</cbc:Name></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="EUR">10.00</cbc:PriceAmount></cac:Price></cac:InvoiceLine>
</Invoice>
""".encode()


def supplier_invoice():
    seller = Party(
        name="Lieferant GmbH",
        street="Weg 9",
        postcode="10115",
        city="Berlin",
        country="DE",
        vat_id="DE123456789",
        email="r@lieferant.example",
        phone="+49 30 1",
        contact_name="L",
    )
    buyer = Party(
        name="Muster Selbstständig", street="Weg 1", postcode="1", city="S", country="DE", email="m@example.org"
    )
    tax = resolve(TaxTreatment.STANDARD, Regime.STANDARD, dt.date(2026, 10, 1))
    return build_invoice(
        number="L-4711",
        issue_date=dt.date(2026, 10, 2),
        service_date=dt.date(2026, 10, 1),
        due_date=dt.date(2026, 10, 16),
        seller=seller,
        buyer=buyer,
        buyer_reference="Kd-Nr 33",
        payment=PaymentInstructions(iban="DE02120300000000202051"),
        lines=[LineData("Druckerpapier", D("2"), "C62", D("12.50"), tax)],
    )


@pytest.fixture
def zugferd_pdf():
    return embed_xml(render_pdf_a3("<h1>L-4711</h1>", title="L-4711"), build_cii(supplier_invoice(), Profile.EN16931))


class TestReader:
    def test_zugferd_pdf(self, zugferd_pdf):
        parsed = parse_einvoice(zugferd_pdf)
        assert (parsed.syntax, parsed.source) == ("cii", "pdf")
        assert (parsed.number, parsed.seller_name, parsed.seller_vat_id) == ("L-4711", "Lieferant GmbH", "DE123456789")
        assert (parsed.net_total, parsed.tax_total, parsed.gross_total) == (D("25.00"), D("4.75"), D("29.75"))
        assert parsed.due_date == dt.date(2026, 10, 16)
        assert parsed.iban == "DE02120300000000202051"
        assert parsed.lines[0].name == "Druckerpapier"
        assert parsed.lines[0].net_amount == D("25.00")

    def test_xrechnung_cii_xml(self):
        parsed = parse_einvoice(build_cii(supplier_invoice(), Profile.XRECHNUNG))
        assert (parsed.syntax, parsed.source, parsed.number) == ("cii", "xml", "L-4711")

    def test_xrechnung_ubl(self):
        parsed = parse_einvoice(UBL)
        assert (parsed.syntax, parsed.number, parsed.seller_name) == ("ubl", "HOST-2026-117", "Hosting AG")
        assert (parsed.net_total, parsed.tax_total, parsed.gross_total) == (D("10.00"), D("1.90"), D("11.90"))
        assert parsed.lines[0].name == "Webhosting Oktober"

    @pytest.mark.parametrize("data", [b"", b"hello", b"%PDF-1.7 ohne XML", b"<html><body>no invoice</body></html>"])
    def test_rejects_non_einvoices(self, data):
        with pytest.raises(EInvoiceParseError):
            parse_einvoice(data)

    def test_rejects_external_entities(self):
        xxe = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><Invoice>&e;</Invoice>'
        with pytest.raises(EInvoiceParseError):
            parse_einvoice(xxe)


@pytest.fixture
def company(db):
    return Company.objects.create(name="Muster Selbstständig", tax_number="1/2/3")


@pytest.mark.django_db
class TestReceive:
    def test_stores_original_and_metadata(self, company, zugferd_pdf):
        incoming = receive(company, zugferd_pdf, filename="L-4711.pdf")
        assert incoming.number == "L-4711" and incoming.gross_total == D("29.75")
        assert incoming.original.mime == "application/pdf"
        assert incoming.original.retention_until == dt.date(2034, 12, 31)
        assert incoming.data["lines"][0]["name"] == "Druckerpapier"

    def test_duplicate_returns_existing(self, company):
        first = receive(company, UBL, filename="a.xml")
        second = receive(company, UBL, filename="b.xml")
        assert first.pk == second.pk
        assert IncomingInvoice.objects.count() == 1

    def test_garbage_is_rejected(self, company):
        with pytest.raises(InboxError):
            receive(company, b"MZ\x90\x00", filename="virus.exe")


@pytest.mark.django_db
def test_upload_and_view(client, company, django_user_model, zugferd_pdf):
    client.force_login(django_user_model.objects.create_user("inhaber", is_staff=True))
    response = client.post("/eingang/", {"file": SimpleUploadedFile("L-4711.pdf", zugferd_pdf, "application/pdf")})
    incoming = IncomingInvoice.objects.get()
    assert response.status_code == 302 and response["Location"] == f"/eingang/{incoming.pk}/"
    body = client.get(f"/eingang/{incoming.pk}/").content.decode()
    assert "Lieferant GmbH" in body and "29,75" in body and "Druckerpapier" in body
    assert client.get(f"/eingang/{incoming.pk}/original/")["Content-Type"] == "application/pdf"
    assert "L-4711" in client.get("/eingang/").content.decode()
