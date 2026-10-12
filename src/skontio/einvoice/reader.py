"""Read incoming e-invoices: ZUGFeRD/Factur-X PDF, XRechnung CII and UBL (Invoice/CreditNote).

Security: XML is parsed without DTDs, entities or network access; documents containing a
DOCTYPE are rejected outright (XXE / billion laughs).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation

import facturx
from lxml import etree

CII = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
UBL = {
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cn": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
MAX_XML_BYTES = 10 * 1024 * 1024
CII_LINE_TOTAL = (
    "ram:SpecifiedLineTradeSettlement/ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount"
)


class EInvoiceParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedLine:
    name: str
    quantity: Decimal | None
    unit_code: str
    net_amount: Decimal | None


@dataclass(frozen=True)
class ParsedInvoice:
    syntax: str  # "cii" | "ubl"
    source: str  # "pdf" | "xml"
    xml: bytes
    number: str
    type_code: str
    issue_date: dt.date | None
    due_date: dt.date | None
    seller_name: str
    seller_vat_id: str
    buyer_name: str
    currency: str
    net_total: Decimal | None
    tax_total: Decimal | None
    gross_total: Decimal | None
    payable: Decimal | None
    iban: str
    lines: list[ParsedLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("xml")
        return _jsonable(data)


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def _parse_xml(data: bytes) -> etree._Element:
    if len(data) > MAX_XML_BYTES:
        raise EInvoiceParseError("XML ist zu groß")
    if b"<!doctype" in data[:4096].lower() or b"<!entity" in data.lower():
        raise EInvoiceParseError("XML mit DOCTYPE/Entitäten wird aus Sicherheitsgründen abgelehnt")
    try:
        return etree.fromstring(data, parser=_PARSER)
    except etree.XMLSyntaxError as exc:
        raise EInvoiceParseError(f"Kein gültiges XML: {exc}") from exc


def _text(node, path: str, ns: dict) -> str:
    return " ".join(node.xpath(f"string({path})", namespaces=ns).split())


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value) if value else None
    except InvalidOperation:
        return None


def _date(value: str, fmt: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(value, fmt).date() if value else None
    except ValueError:
        return None


def _parse_cii(root, source: str, xml: bytes) -> ParsedInvoice:
    def t(path: str) -> str:
        return _text(root, path, CII)

    total = "//ram:SpecifiedTradeSettlementHeaderMonetarySummation"
    lines = [
        ParsedLine(
            name=_text(item, "ram:SpecifiedTradeProduct/ram:Name", CII),
            quantity=_decimal(_text(item, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity", CII)),
            unit_code=item.xpath("string(ram:SpecifiedLineTradeDelivery/ram:BilledQuantity/@unitCode)", namespaces=CII),
            net_amount=_decimal(_text(item, CII_LINE_TOTAL, CII)),
        )
        for item in root.xpath("//ram:IncludedSupplyChainTradeLineItem", namespaces=CII)
    ]
    return ParsedInvoice(
        syntax="cii",
        source=source,
        xml=xml,
        number=t("/rsm:CrossIndustryInvoice/rsm:ExchangedDocument/ram:ID"),
        type_code=t("/rsm:CrossIndustryInvoice/rsm:ExchangedDocument/ram:TypeCode"),
        issue_date=_date(t("//rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString"), "%Y%m%d"),
        due_date=_date(t("//ram:SpecifiedTradePaymentTerms/ram:DueDateDateTime/udt:DateTimeString"), "%Y%m%d"),
        seller_name=t("//ram:SellerTradeParty/ram:Name"),
        seller_vat_id=t("//ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']"),
        buyer_name=t("//ram:BuyerTradeParty/ram:Name"),
        currency=t("//ram:InvoiceCurrencyCode"),
        net_total=_decimal(t(f"{total}/ram:TaxBasisTotalAmount")),
        tax_total=_decimal(t(f"{total}/ram:TaxTotalAmount")),
        gross_total=_decimal(t(f"{total}/ram:GrandTotalAmount")),
        payable=_decimal(t(f"{total}/ram:DuePayableAmount")),
        iban=t("//ram:PayeePartyCreditorFinancialAccount/ram:IBANID"),
        lines=lines,
    )


def _parse_ubl(root, source: str, xml: bytes) -> ParsedInvoice:
    def t(path: str) -> str:
        return _text(root, path, UBL)

    supplier = "/*/cac:AccountingSupplierParty/cac:Party"
    customer = "/*/cac:AccountingCustomerParty/cac:Party"
    totals = "/*/cac:LegalMonetaryTotal"
    lines = [
        ParsedLine(
            name=_text(line, "cac:Item/cbc:Name", UBL),
            quantity=_decimal(_text(line, "cbc:InvoicedQuantity | cbc:CreditedQuantity", UBL)),
            unit_code=line.xpath("string((cbc:InvoicedQuantity | cbc:CreditedQuantity)/@unitCode)", namespaces=UBL),
            net_amount=_decimal(_text(line, "cbc:LineExtensionAmount", UBL)),
        )
        for line in root.xpath("/*/cac:InvoiceLine | /*/cac:CreditNoteLine", namespaces=UBL)
    ]
    return ParsedInvoice(
        syntax="ubl",
        source=source,
        xml=xml,
        number=t("/*/cbc:ID"),
        type_code=t("/*/cbc:InvoiceTypeCode | /*/cbc:CreditNoteTypeCode"),
        issue_date=_date(t("/*/cbc:IssueDate"), "%Y-%m-%d"),
        due_date=_date(t("/*/cbc:DueDate") or t("/*/cac:PaymentMeans/cbc:PaymentDueDate"), "%Y-%m-%d"),
        seller_name=t(f"{supplier}/cac:PartyLegalEntity/cbc:RegistrationName")
        or t(f"{supplier}/cac:PartyName/cbc:Name"),
        seller_vat_id=t(f"{supplier}/cac:PartyTaxScheme[cac:TaxScheme/cbc:ID='VAT']/cbc:CompanyID"),
        buyer_name=t(f"{customer}/cac:PartyLegalEntity/cbc:RegistrationName")
        or t(f"{customer}/cac:PartyName/cbc:Name"),
        currency=t("/*/cbc:DocumentCurrencyCode"),
        net_total=_decimal(t(f"{totals}/cbc:TaxExclusiveAmount")),
        tax_total=_decimal(t("/*/cac:TaxTotal/cbc:TaxAmount")),
        gross_total=_decimal(t(f"{totals}/cbc:TaxInclusiveAmount")),
        payable=_decimal(t(f"{totals}/cbc:PayableAmount")),
        iban=t("/*/cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID"),
        lines=lines,
    )


def _with_plausibility_warnings(invoice: ParsedInvoice) -> ParsedInvoice:
    warnings = []
    if not invoice.number:
        warnings.append("Keine Rechnungsnummer gefunden")
    if None not in (invoice.net_total, invoice.tax_total, invoice.gross_total):
        if invoice.net_total + invoice.tax_total != invoice.gross_total:
            warnings.append("Netto + Steuer ergibt nicht den Bruttobetrag")
    return replace(invoice, warnings=warnings)


def parse_einvoice(data: bytes) -> ParsedInvoice:
    if data[:5] == b"%PDF-":
        try:
            _, xml = facturx.get_xml_from_pdf(data, check_xsd=False)
        except Exception as exc:  # noqa: BLE001 - factur-x raises generic errors on broken PDFs
            raise EInvoiceParseError(f"PDF konnte nicht gelesen werden: {exc}") from exc
        if not xml:
            raise EInvoiceParseError("Das PDF enthält keine eingebettete E-Rechnung (ZUGFeRD/Factur-X)")
        source = "pdf"
    elif data.lstrip()[:1] == b"<":
        xml, source = data, "xml"
    else:
        raise EInvoiceParseError("Weder PDF noch XML")

    root = _parse_xml(xml)
    namespace = etree.QName(root).namespace
    if namespace == CII["rsm"]:
        return _with_plausibility_warnings(_parse_cii(root, source, xml))
    if namespace in (UBL["inv"], UBL["cn"]):
        return _with_plausibility_warnings(_parse_ubl(root, source, xml))
    raise EInvoiceParseError("Unbekanntes XML-Format – erwartet CII (ZUGFeRD/XRechnung) oder UBL")
