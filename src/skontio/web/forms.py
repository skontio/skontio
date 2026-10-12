from __future__ import annotations

import datetime as dt
import re

from django import forms

from skontio.catalog.models import Item, Unit
from skontio.core.models import Company
from skontio.crm.models import Customer
from skontio.crm.services import normalise_vat_id
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


class ItemForm(forms.ModelForm):
    unit = forms.ModelChoiceField(queryset=Unit.objects.all(), to_field_name="code", label="Einheit")
    net_price = forms.DecimalField(label="Nettopreis (€)", min_value=0, decimal_places=4, localize=True)
    tax_treatment = forms.ChoiceField(label="Steuerbehandlung", choices=list(TAX_TREATMENT_LABELS.items()))

    class Meta:
        model = Item
        fields = ["kind", "name", "sku", "description", "unit", "net_price", "tax_treatment"]
