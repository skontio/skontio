"""UN/CEFACT CII writer (EN 16931) for XRechnung and ZUGFeRD/Factur-X profile EN 16931."""

from __future__ import annotations

import enum

from drafthorse.models.accounting import ApplicableTradeTax
from drafthorse.models.document import Document
from drafthorse.models.note import IncludedNote
from drafthorse.models.party import TaxRegistration
from drafthorse.models.payment import PaymentMeans, PaymentTerms
from drafthorse.models.tradelines import LineItem

from skontio.einvoice.model import Invoice, Party


class Profile(enum.StrEnum):
    # BT-24 specification identifiers
    XRECHNUNG = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
    EN16931 = "urn:cen.eu:en16931:2017"


PEPPOL_BILLING_PROCESS = "urn:fdc:peppol.eu:2017:poacc:billing:01.0"  # BT-23


class EInvoiceDataError(ValueError):
    """Mandatory EN 16931 / XRechnung data is missing."""


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
        item.agreement.net.amount = line.unit_price
        item.delivery.billed_quantity = (line.quantity, line.unit_code)
        item.settlement.trade_tax.type_code = "VAT"
        item.settlement.trade_tax.category_code = invoice.tax_category
        item.settlement.trade_tax.rate_applicable_percent = invoice.tax_rate
        item.settlement.monetary_summation.total_amount = line.net_amount
        doc.trade.items.add(item)

    settlement = doc.trade.settlement
    settlement.currency_code = invoice.currency
    settlement.payment_reference = invoice.number

    means = PaymentMeans(type_code=invoice.payment.means_code)
    means.payee_account.iban = invoice.payment.iban
    if invoice.payment.account_name:
        means.payee_account.account_name = invoice.payment.account_name
    if invoice.payment.bic:
        means.payee_institution.bic = invoice.payment.bic
    settlement.payment_means.add(means)

    tax = ApplicableTradeTax()
    tax.calculated_amount = invoice.tax_total
    tax.type_code = "VAT"
    if invoice.tax_exemption_reason:
        tax.exemption_reason = invoice.tax_exemption_reason
    tax.basis_amount = invoice.line_total
    tax.category_code = invoice.tax_category
    tax.rate_applicable_percent = invoice.tax_rate
    settlement.trade_tax.add(tax)

    if invoice.payment_terms or invoice.due_date:
        terms = PaymentTerms()
        if invoice.payment_terms:
            terms.description = invoice.payment_terms
        if invoice.due_date:
            terms.due = invoice.due_date
        settlement.terms.add(terms)

    totals = settlement.monetary_summation
    totals.line_total = invoice.line_total
    totals.tax_basis_total = invoice.line_total
    totals.tax_total = (invoice.tax_total, invoice.currency)
    totals.grand_total = invoice.grand_total
    totals.due_amount = invoice.grand_total

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
    if not invoice.lines:
        missing.append("Positionen")
    if not invoice.service_date:
        missing.append("Leistungsdatum (§ 14 Abs. 4 Nr. 6 UStG, BT-72)")
    if not (invoice.seller.vat_id or invoice.seller.tax_number):
        missing.append("Steuernummer oder USt-IdNr. des Verkäufers (BR-DE-16)")
    if not (invoice.seller.vat_id or invoice.seller.identifier):
        missing.append("USt-IdNr. (BT-31) oder Verkäuferkennung (BT-29) des Verkäufers (BR-CO-26)")
    if invoice.tax_category in {"E", "AE", "K", "G", "O"} and not invoice.tax_exemption_reason:
        missing.append("Befreiungsgrund (BT-120)")
    if not (invoice.due_date or invoice.payment_terms):
        missing.append("Fälligkeit oder Zahlungsbedingungen (BR-CO-25)")
    if profile is Profile.XRECHNUNG:
        seller = invoice.seller
        if not (seller.contact_name and seller.phone and seller.email):
            missing.append("Kontakt des Verkäufers: Name, Telefon, E-Mail (BR-DE-2)")
        if not invoice.buyer.email:
            missing.append("Elektronische Adresse des Käufers (BT-49)")
        if not invoice.buyer_reference:
            missing.append("Käuferreferenz/Leitweg-ID (BT-10)")
    if missing:
        raise EInvoiceDataError("Fehlende Pflichtangaben: " + "; ".join(missing))
