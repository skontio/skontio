"""UN/CEFACT CII writer (EN 16931) for XRechnung and ZUGFeRD/Factur-X profile EN 16931."""

from __future__ import annotations

import enum
from decimal import Decimal

from drafthorse.models.accounting import ApplicableTradeTax, CategoryTradeTax, TradeAllowanceCharge
from drafthorse.models.document import Document
from drafthorse.models.note import IncludedNote
from drafthorse.models.party import TaxRegistration
from drafthorse.models.payment import PaymentMeans, PaymentTerms
from drafthorse.models.tradelines import LineItem

from skontio.einvoice.model import Invoice, Party

PERCENT = Decimal("0.01")
ALLOWANCE_REASON = "Rabatt"
ALLOWANCE_REASON_CODE = "95"  # UNCL5189: Discount


class Profile(enum.StrEnum):
    # BT-24 specification identifiers
    XRECHNUNG = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
    EN16931 = "urn:cen.eu:en16931:2017"


PEPPOL_BILLING_PROCESS = "urn:fdc:peppol.eu:2017:poacc:billing:01.0"  # BT-23


class EInvoiceDataError(ValueError):
    """Mandatory EN 16931 / XRechnung data is missing."""


def _rate(value: Decimal) -> Decimal:
    return value.quantize(PERCENT)


def build_cii(invoice: Invoice, profile: Profile) -> bytes:
    _check_mandatory(invoice, profile)
    doc = Document()
    doc.context.guideline_parameter.id = profile.value
    if profile is Profile.XRECHNUNG:
        doc.context.business_parameter.id = PEPPOL_BILLING_PROCESS

    doc.header.id = invoice.number
    doc.header.type_code = invoice.type_code
    doc.header.issue_date_time = invoice.issue_date
    for note in invoice.notes:
        doc.header.notes.add(IncludedNote(content=note))

    agreement = doc.trade.agreement
    agreement.buyer_reference = invoice.buyer_reference
    _fill_party(agreement.seller, invoice.seller, is_seller=True)
    _fill_party(agreement.buyer, invoice.buyer, is_seller=False)

    if invoice.service_date:
        doc.trade.delivery.event.occurrence = invoice.service_date

    for index, line in enumerate(invoice.lines, start=1):
        item = LineItem()
        item.document.line_id = str(index)
        item.product.name = line.name
        if line.description:
            item.product.description = line.description
        item.agreement.net.amount = line.unit_price
        item.delivery.billed_quantity = (line.quantity, line.unit_code)
        item.settlement.trade_tax.type_code = "VAT"
        item.settlement.trade_tax.category_code = line.tax_category
        item.settlement.trade_tax.rate_applicable_percent = _rate(line.tax_rate)
        if line.discount_amount:
            allowance = TradeAllowanceCharge()
            allowance.indicator = False
            allowance.actual_amount = line.discount_amount
            allowance.reason_code = ALLOWANCE_REASON_CODE
            allowance.reason = ALLOWANCE_REASON
            item.settlement.allowance_charge.add(allowance)
        item.settlement.monetary_summation.total_amount = line.net_amount
        doc.trade.items.add(item)

    settlement = doc.trade.settlement
    settlement.currency_code = invoice.currency
    settlement.payment_reference = invoice.number
    if invoice.service_period:
        settlement.period.start, settlement.period.end = invoice.service_period
    if invoice.preceding_invoice:
        settlement.invoice_referenced_document.issuer_assigned_id = invoice.preceding_invoice.number
        settlement.invoice_referenced_document.issue_date_time = invoice.preceding_invoice.issue_date

    means = PaymentMeans(type_code=invoice.payment.means_code)
    means.payee_account.iban = invoice.payment.iban
    if invoice.payment.account_name:
        means.payee_account.account_name = invoice.payment.account_name
    if invoice.payment.bic:
        means.payee_institution.bic = invoice.payment.bic
    settlement.payment_means.add(means)

    for subtotal in invoice.tax_subtotals:
        tax = ApplicableTradeTax()
        tax.calculated_amount = subtotal.tax
        tax.type_code = "VAT"
        if subtotal.exemption_reason:
            tax.exemption_reason = subtotal.exemption_reason
        tax.basis_amount = subtotal.basis
        tax.category_code = subtotal.category
        tax.rate_applicable_percent = _rate(subtotal.rate)
        settlement.trade_tax.add(tax)

        if subtotal.allowance:  # BG-20: document-level allowance per VAT category
            allowance = TradeAllowanceCharge()
            allowance.indicator = False
            allowance.actual_amount = subtotal.allowance
            allowance.reason_code = ALLOWANCE_REASON_CODE
            allowance.reason = ALLOWANCE_REASON
            category = CategoryTradeTax()
            category.type_code = "VAT"
            category.category_code = subtotal.category
            category.rate_applicable_percent = _rate(subtotal.rate)
            allowance.trade_tax.add(category)
            settlement.allowance_charge.add(allowance)

    if invoice.payment_terms or invoice.due_date:
        terms = PaymentTerms()
        if invoice.payment_terms:
            terms.description = invoice.payment_terms
        if invoice.due_date:
            terms.due = invoice.due_date
        settlement.terms.add(terms)

    totals = settlement.monetary_summation
    totals.line_total = invoice.line_total
    if invoice.allowance_total:
        totals.allowance_total = invoice.allowance_total
    totals.tax_basis_total = invoice.tax_basis_total
    totals.tax_total = (invoice.tax_total, invoice.currency)
    totals.grand_total = invoice.grand_total
    if invoice.prepaid_amount:
        totals.prepaid_total = invoice.prepaid_amount
    totals.due_amount = invoice.due_amount

    return doc.serialize(schema="FACTUR-X_EN16931")


def _fill_party(target, party: Party, *, is_seller: bool) -> None:
    if party.identifier:
        target.id = party.identifier
    target.name = party.name
    target.address.line_one = party.street
    target.address.postcode = party.postcode
    target.address.city_name = party.city
    target.address.country_id = party.country
    if party.email:
        target.electronic_address.uri_ID = ("EM", party.email)  # BT-34 / BT-49
    if party.vat_id:
        target.tax_registrations.add(TaxRegistration(id=("VA", party.vat_id)))
    if is_seller and party.tax_number:  # BT-32 exists for the seller only; buyers carry BT-48 (VA)
        target.tax_registrations.add(TaxRegistration(id=("FC", party.tax_number)))
    if is_seller:
        if party.contact_name:
            target.contact.person_name = party.contact_name
        if party.phone:
            target.contact.telephone.number = party.phone
        if party.email:
            target.contact.email.address = party.email


def _check_mandatory(invoice: Invoice, profile: Profile) -> None:
    missing: list[str] = []
    seller, buyer = invoice.seller, invoice.buyer
    categories = {s.category for s in invoice.tax_subtotals}
    if not invoice.lines:
        missing.append("Positionen")
    if not (invoice.service_date or invoice.service_period):
        missing.append("Leistungsdatum oder Leistungszeitraum (§ 14 Abs. 4 Nr. 6 UStG, BT-72/BG-14)")
    if not (seller.vat_id or seller.tax_number):
        missing.append("Steuernummer oder USt-IdNr. des Verkäufers (BR-DE-16)")
    if not (seller.vat_id or seller.identifier):
        missing.append("USt-IdNr. (BT-31) oder Verkäuferkennung (BT-29) des Verkäufers (BR-CO-26)")
    for subtotal in invoice.tax_subtotals:
        if subtotal.category in {"E", "AE", "K", "G", "O"} and not subtotal.exemption_reason:
            missing.append(f"Befreiungsgrund für Kategorie {subtotal.category} (BT-120)")
    if categories & {"AE", "K"} and not (seller.vat_id and buyer.vat_id):
        missing.append("USt-IdNr. von Verkäufer und Käufer bei Reverse Charge/ig. Lieferung (BR-AE-02, BR-IC-02)")
    if invoice.type_code == "381" and not invoice.preceding_invoice:
        missing.append("Verweis auf die stornierte Rechnung (BG-3)")
    if not (invoice.due_date or invoice.payment_terms):
        missing.append("Fälligkeit oder Zahlungsbedingungen (BR-CO-25)")
    if profile is Profile.XRECHNUNG:
        if not (seller.contact_name and seller.phone and seller.email):
            missing.append("Kontakt des Verkäufers: Name, Telefon, E-Mail (BR-DE-2)")
        if not buyer.email:
            missing.append("Elektronische Adresse des Käufers (BT-49)")
        if not invoice.buyer_reference:
            missing.append("Käuferreferenz/Leitweg-ID (BT-10)")
    if missing:
        raise EInvoiceDataError("Fehlende Pflichtangaben: " + "; ".join(missing))
