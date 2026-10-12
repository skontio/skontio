from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from django import forms

from skontio.catalog.models import Item, Unit
from skontio.core.models import Company
from skontio.crm.models import Customer
from skontio.crm.services import normalise_vat_id
from skontio.payments.models import Payment
from skontio.tax.engine import Regime, TaxTreatment

TAX_TREATMENT_LABELS = {
    TaxTreatment.STANDARD: "Regelsatz (19 %)",
    TaxTreatment.REDUCED: "Ermäßigt (7 %)",
    TaxTreatment.ZERO: "Nullsatz (§ 12 Abs. 3 UStG)",
    TaxTreatment.EXEMPT: "Steuerfrei (§ 4 UStG)",
    TaxTreatment.REVERSE_CHARGE: "Reverse Charge (§ 13b UStG)",
    TaxTreatment.INTRA_EU_SUPPLY: "Innergemeinschaftliche Lieferung",
    TaxTreatment.EXPORT: "Ausfuhrlieferung",
    TaxTreatment.NOT_TAXABLE: "Nicht steuerbar",
}


def normalise_iban(raw: str) -> str:
    iban = re.sub(r"\s", "", raw).upper()
    if not iban:
        return ""
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban):
        raise forms.ValidationError("Die IBAN hat kein gültiges Format.")
    digits = "".join(str(int(ch, 36)) for ch in iban[4:] + iban[:4])
    if int(digits) % 97 != 1:
        raise forms.ValidationError("Die IBAN-Prüfsumme stimmt nicht.")
    return iban


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = [
            "name",
            "street",
            "postcode",
            "city",
            "country",
            "activity",
            "side_business",
            "tax_number",
            "vat_id",
            "seller_identifier",
            "email",
            "phone",
            "contact_name",
            "iban",
            "bic",
            "account_holder",
        ]

    def clean_iban(self):
        return normalise_iban(self.cleaned_data["iban"])

    def clean_vat_id(self):
        try:
            return normalise_vat_id(self.cleaned_data["vat_id"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean(self):
        data = super().clean()
        if not (data.get("tax_number") or data.get("vat_id")):
            raise forms.ValidationError("Steuernummer oder USt-IdNr. ist für Rechnungen Pflicht (§ 14 UStG).")
        return data


class SetupForm(CompanyForm):
    regime = forms.ChoiceField(
        label="Besteuerung",
        choices=[
            (Regime.SMALL_BUSINESS.value, "Kleinunternehmer (§ 19 UStG)"),
            (Regime.STANDARD.value, "Regelbesteuerung"),
        ],
    )
    regime_since = forms.DateField(label="Gilt seit", initial=lambda: dt.date(dt.date.today().year, 1, 1))


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "kind",
            "name",
            "street",
            "postcode",
            "city",
            "country",
            "email",
            "phone",
            "vat_id",
            "buyer_reference",
            "language",
            "payment_terms_days",
        ]

    def clean_vat_id(self):
        try:
            return normalise_vat_id(self.cleaned_data["vat_id"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc


class NewDocumentForm(forms.Form):
    doc_type = forms.ChoiceField(
        label="Art",
        choices=[
            ("invoice", "Rechnung"),
            ("quote", "Angebot"),
            ("order_confirmation", "Auftragsbestätigung"),
            ("partial_invoice", "Abschlagsrechnung"),
            ("final_invoice", "Schlussrechnung"),
        ],
    )
    customer = forms.ModelChoiceField(queryset=Customer.objects.none(), label="Kunde", empty_label="– Kunde wählen –")
    issue_date = forms.DateField(label="Datum", initial=dt.date.today)
    service_date = forms.DateField(label="Leistungsdatum", required=False)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.filter(company=company, archived_at__isnull=True)


class HeaderForm(forms.Form):
    issue_date = forms.DateField(label="Datum")
    service_date = forms.DateField(label="Leistungsdatum", required=False)
    service_period_start = forms.DateField(label="Leistungszeitraum von", required=False)
    service_period_end = forms.DateField(label="bis", required=False)
    due_date = forms.DateField(label="Fällig am", required=False)
    valid_until = forms.DateField(label="Gültig bis", required=False)
    document_discount_percent = forms.DecimalField(
        label="Rabatt auf alles (%)", min_value=0, max_value=100, decimal_places=2, localize=True, required=False
    )
    intro_text = forms.CharField(label="Einleitung", widget=forms.Textarea(attrs={"rows": 2}), required=False)
    closing_text = forms.CharField(label="Schlusstext", widget=forms.Textarea(attrs={"rows": 2}), required=False)

    def clean(self):
        data = super().clean()
        start, end = data.get("service_period_start"), data.get("service_period_end")
        if bool(start) != bool(end):
            raise forms.ValidationError("Für einen Leistungszeitraum Beginn und Ende angeben.")
        if start and data.get("service_date"):
            raise forms.ValidationError("Entweder Leistungsdatum oder Leistungszeitraum angeben, nicht beides.")
        if start and end and end < start:
            raise forms.ValidationError("Der Leistungszeitraum endet vor seinem Beginn.")
        return data


class LineForm(forms.Form):
    item = forms.ModelChoiceField(
        queryset=Item.objects.none(), required=False, label="Aus dem Katalog", empty_label="– freie Position –"
    )
    text = forms.CharField(required=False, label="Bezeichnung")
    quantity = forms.DecimalField(label="Menge", min_value=0, decimal_places=4, localize=True)
    unit_code = forms.ModelChoiceField(
        queryset=Unit.objects.all(),
        to_field_name="code",
        required=False,
        label="Einheit",
        empty_label="Stück (Standard)",
    )
    unit_price = forms.DecimalField(
        label="Einzelpreis (€)", min_value=0, decimal_places=4, localize=True, required=False
    )
    discount_percent = forms.DecimalField(
        label="Rabatt (%)", min_value=0, max_value=100, decimal_places=2, localize=True, required=False
    )
    tax_treatment = forms.ChoiceField(
        label="Steuer", choices=list(TAX_TREATMENT_LABELS.items()), initial=TaxTreatment.STANDARD, required=False
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = Item.objects.filter(company=company, archived_at__isnull=True)

    def clean(self):
        data = super().clean()
        if not data.get("item"):
            if not (data.get("text") or "").strip():
                self.add_error("text", "Bitte eine Bezeichnung angeben oder eine Leistung aus dem Katalog wählen.")
            if data.get("unit_price") is None:
                self.add_error("unit_price", "Bitte einen Einzelpreis angeben.")
        return data


class PaymentForm(forms.Form):
    amount = forms.DecimalField(label="Betrag (€)", min_value=Decimal("0.01"), decimal_places=2, localize=True)
    received_on = forms.DateField(label="Eingegangen am", initial=dt.date.today)
    method = forms.ChoiceField(label="Zahlungsart", choices=Payment.Method.choices)
    note = forms.CharField(label="Notiz", required=False)


class SendForm(forms.Form):
    to = forms.CharField(label="An", help_text="Mehrere Adressen mit Komma trennen")
    cc = forms.CharField(label="Kopie an", required=False)
    subject = forms.CharField(label="Betreff", max_length=300)
    body = forms.CharField(label="Nachricht", widget=forms.Textarea(attrs={"rows": 8}))

    @staticmethod
    def split(value: str) -> list[str]:
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


class CancelForm(forms.Form):
    reason = forms.CharField(label="Grund für den Storno", max_length=300)


class ConvertForm(forms.Form):
    service_date = forms.DateField(label="Leistungsdatum", required=False)


class PartialForm(forms.Form):
    percent = forms.DecimalField(
        label="Abschlag in %", required=False, min_value=0, max_value=100, decimal_places=2, localize=True
    )
    net_amount = forms.DecimalField(
        label="oder Nettobetrag (€)", required=False, min_value=0, decimal_places=2, localize=True
    )
    service_date = forms.DateField(label="Leistungsdatum", required=False)

    def clean(self):
        data = super().clean()
        if (data.get("percent") is None) == (data.get("net_amount") is None):
            raise forms.ValidationError("Entweder einen Prozentsatz oder einen Nettobetrag angeben.")
        return data


class ItemForm(forms.ModelForm):
    unit = forms.ModelChoiceField(
        queryset=Unit.objects.all(), to_field_name="code", label="Einheit", empty_label="– Einheit wählen –"
    )
    net_price = forms.DecimalField(label="Nettopreis (€)", min_value=0, decimal_places=4, localize=True)
    tax_treatment = forms.ChoiceField(label="Steuerbehandlung", choices=list(TAX_TREATMENT_LABELS.items()))

    class Meta:
        model = Item
        fields = ["kind", "name", "sku", "description", "unit", "net_price", "tax_treatment"]
