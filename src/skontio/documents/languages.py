"""Document languages: wording and number/date format of PDFs, e-invoice notes and e-mails.

The user interface stays German; only what the customer receives follows ``Document.language``.
Tax notes are translated once when the document is finalized and then frozen in the snapshot.
§ 14 UStG does not prescribe a language; the German legal reference stays in every translation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from skontio.core.models import DOCUMENT_LANGUAGES
from skontio.documents.models import Document
from skontio.tax import de
from skontio.tax.engine import TaxTreatment

Type = Document.Type
DEFAULT = "de"
LANGUAGES = dict(DOCUMENT_LANGUAGES)

TEXTS = {
    "de": {
        "titles": {
            Type.QUOTE: "Angebot",
            Type.ORDER_CONFIRMATION: "Auftragsbestätigung",
            Type.INVOICE: "Rechnung",
            Type.PARTIAL_INVOICE: "Abschlagsrechnung",
            Type.FINAL_INVOICE: "Schlussrechnung",
            Type.CANCELLATION: "Stornorechnung",
        },
        "basis_prefix": {Type.QUOTE: "zum Angebot", Type.ORDER_CONFIRMATION: "zur Auftragsbestätigung"},
        "units": {"C62": "Stk.", "HUR": "Std.", "DAY": "Tag(e)", "MON": "Monat(e)", "LS": "pauschal", "KMT": "km"},
        "number": "{title}-Nr.",
        "date": "Datum",
        "service_date": "Leistungsdatum",
        "service_period": "Leistungszeitraum",
        "customer_number": "Kundennummer",
        "buyer_vat_id": "USt-IdNr. Kunde",
        "valid_until": "Gültig bis",
        "due_date": "Fällig am",
        "col_position": "Pos.",
        "col_text": "Bezeichnung",
        "col_quantity": "Menge",
        "col_unit": "Einheit",
        "col_price": "Einzelpreis",
        "col_discount": "Rabatt",
        "col_amount": "Betrag",
        "col_vat": "USt",
        "line_total": "Summe Positionen",
        "discount": "Rabatt",
        "net_total": "Nettobetrag",
        "vat_on": "USt {rate} auf {basis}",
        "grand_total": "Gesamtbetrag",
        "invoice_total": "Rechnungsbetrag",
        "less_partial": "abzüglich Abschlagsrechnung {number} vom {date}",
        "deduction_detail": "netto {net} · USt {rate} {tax}",
        "amount_due": "Noch zu zahlen",
        "credit_due": "Gutschrift (Restbetrag)",
        "pay_by": "Bitte überweisen Sie {amount} bis zum {date}.",
        "payment_reference": "Verwendungszweck",
        "tax_number": "Steuernummer",
        "vat_id": "USt-IdNr.",
        "basis": "{prefix} {number} vom {date}",
        "cancellation_of": "Stornierung der Rechnung {number} vom {date}",
        "reason": "Grund: {reason}",
        "partial_of": "Abschlagsrechnung {basis}",
        "final_of": "Schlussrechnung {basis}",
        "deduction_note": "Abzüglich Abschlagsrechnung {number} vom {date}: Entgelt {net} EUR, "
        "Umsatzsteuer {rate} % {tax} EUR",
        "credit_terms": "Der Rechnungsbetrag wird gutgeschrieben bzw. erstattet.",
        "payable_by": "Zahlbar ohne Abzug bis {date}.",
        "mail_subject": "{title} {number} von {company}",
        "mail_greeting": "Guten Tag,",
        "mail_attached": "anbei erhalten Sie unsere {title} {number}.",
        "mail_pay": "Bitte überweisen Sie {amount} bis zum {date} unter Angabe der Rechnungsnummer.",
        "mail_einvoice": "Die angehängte PDF-Datei enthält die E-Rechnung im Format ZUGFeRD (EN 16931).",
        "mail_regards": "Mit freundlichen Grüßen",
        "down_payment_percent": "Abschlag {percent} % gemäß {title} {number}",
        "down_payment": "Abschlag gemäß {title} {number}",
        "treatments": {
            TaxTreatment.STANDARD: "Regelsatz",
            TaxTreatment.REDUCED: "ermäßigter Satz",
            TaxTreatment.ZERO: "Nullsatz",
            TaxTreatment.EXEMPT: "steuerfrei",
            TaxTreatment.REVERSE_CHARGE: "Steuerschuldnerschaft des Leistungsempfängers",
            TaxTreatment.INTRA_EU_SUPPLY: "innergemeinschaftliche Lieferung",
            TaxTreatment.EXPORT: "Ausfuhr",
            TaxTreatment.NOT_TAXABLE: "nicht steuerbar",
        },
    },
    "en": {
        "titles": {
            Type.QUOTE: "Quotation",
            Type.ORDER_CONFIRMATION: "Order Confirmation",
            Type.INVOICE: "Invoice",
            Type.PARTIAL_INVOICE: "Partial Invoice",
            Type.FINAL_INVOICE: "Final Invoice",
            Type.CANCELLATION: "Cancellation Invoice",
        },
        "basis_prefix": {Type.QUOTE: "for quotation", Type.ORDER_CONFIRMATION: "for order confirmation"},
        "units": {"C62": "pcs", "HUR": "h", "DAY": "day(s)", "MON": "month(s)", "LS": "lump sum", "KMT": "km"},
        "number": "{title} No.",
        "date": "Date",
        "service_date": "Date of supply",
        "service_period": "Period of supply",
        "customer_number": "Customer No.",
        "buyer_vat_id": "Customer VAT ID",
        "valid_until": "Valid until",
        "due_date": "Due date",
        "col_position": "No.",
        "col_text": "Description",
        "col_quantity": "Qty",
        "col_unit": "Unit",
        "col_price": "Unit price",
        "col_discount": "Discount",
        "col_amount": "Amount",
        "col_vat": "VAT",
        "line_total": "Subtotal",
        "discount": "Discount",
        "net_total": "Net amount",
        "vat_on": "VAT {rate} on {basis}",
        "grand_total": "Total",
        "invoice_total": "Invoice total",
        "less_partial": "less partial invoice {number} of {date}",
        "deduction_detail": "net {net} · VAT {rate} {tax}",
        "amount_due": "Amount due",
        "credit_due": "Credit (remaining amount)",
        "pay_by": "Please transfer {amount} by {date}.",
        "payment_reference": "Payment reference",
        "tax_number": "Tax number",
        "vat_id": "VAT ID",
        "basis": "{prefix} {number} of {date}",
        "cancellation_of": "Cancellation of invoice {number} of {date}",
        "reason": "Reason: {reason}",
        "partial_of": "Partial invoice {basis}",
        "final_of": "Final invoice {basis}",
        "deduction_note": "Less partial invoice {number} of {date}: net amount {net} EUR, VAT {rate} % {tax} EUR",
        "credit_terms": "The invoice amount will be credited or refunded.",
        "payable_by": "Payable without deduction by {date}.",
        "mail_subject": "{title} {number} from {company}",
        "mail_greeting": "Hello,",
        "mail_attached": "please find attached our {title} {number}.",
        "mail_pay": "Please transfer {amount} by {date}, quoting the invoice number.",
        "mail_einvoice": "The attached PDF contains the e-invoice in ZUGFeRD format (EN 16931).",
        "mail_regards": "Kind regards",
        "down_payment_percent": "Down payment of {percent}% as per {title} {number}",
        "down_payment": "Down payment as per {title} {number}",
        "treatments": {
            TaxTreatment.STANDARD: "standard rate",
            TaxTreatment.REDUCED: "reduced rate",
            TaxTreatment.ZERO: "zero rate",
            TaxTreatment.EXEMPT: "exempt",
            TaxTreatment.REVERSE_CHARGE: "reverse charge",
            TaxTreatment.INTRA_EU_SUPPLY: "intra-Community supply",
            TaxTreatment.EXPORT: "export",
            TaxTreatment.NOT_TAXABLE: "not taxable",
        },
    },
}

TAX_NOTES = {
    "en": {
        de.SMALL_BUSINESS_NOTE: "VAT exemption for small businesses under § 19 UStG (German VAT Act) – "
        "no VAT is charged.",
        de.EXEMPT_NOTE: "VAT-exempt supply under § 4 UStG.",
        de.REVERSE_CHARGE_NOTE: "Reverse charge – the recipient is liable for VAT "
        "(Steuerschuldnerschaft des Leistungsempfängers, § 13b UStG).",
        de.INTRA_EU_SUPPLY_NOTE: "VAT-exempt intra-Community supply (§ 4 Nr. 1 Buchst. b, § 6a UStG).",
        de.EXPORT_NOTE: "VAT-exempt export supply (§ 4 Nr. 1 Buchst. a, § 6 UStG).",
        de.NOT_TAXABLE_NOTE: "Supply not taxable in Germany.",
    }
}
MONTHS_EN = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def normalise(language: str | None) -> str:
    return language if language in LANGUAGES else DEFAULT


class Lang:
    """Wording and formatting for one document language."""

    def __init__(self, language: str | None):
        self.code = normalise(language)
        self.texts = TEXTS[self.code]

    def __call__(self, key: str, **values) -> str:
        return self.texts[key].format(**values)

    def title(self, doc_type) -> str:
        return self.texts["titles"][Type(doc_type)]

    def treatment(self, treatment) -> str:
        return self.texts["treatments"][TaxTreatment(treatment)]

    def unit(self, code: str) -> str:
        return self.texts["units"].get(code, code)

    def basis(self, basis: Document) -> str:
        prefix = self.texts["basis_prefix"].get(Type(basis.doc_type), "")
        return self("basis", prefix=prefix, number=basis.number, date=self.date(basis.issue_date)).strip()

    def tax_note(self, note: str) -> str:
        return TAX_NOTES.get(self.code, {}).get(note, note)

    def date(self, value: dt.date | None) -> str:
        if value is None:
            return ""
        if self.code == "en":
            return f"{value.day} {MONTHS_EN[value.month - 1]} {value.year}"
        return f"{value:%d.%m.%Y}"

    def amount(self, value: Decimal, places: int = 2) -> str:
        text = f"{value:,.{places}f}"
        if self.code == "en":
            return text
        return text.replace(",", " ").replace(".", ",").replace(" ", ".")

    def quantity(self, value: Decimal) -> str:
        places = max(0, -value.normalize().as_tuple().exponent)
        return self.amount(value, places)

    def percent(self, value: Decimal) -> str:
        return f"{self.quantity(value)} %" if self.code == "de" else f"{self.quantity(value)}%"

    def money(self, value: Decimal) -> str:
        return f"€{self.amount(value)}" if self.code == "en" else f"{self.amount(value)} €"
