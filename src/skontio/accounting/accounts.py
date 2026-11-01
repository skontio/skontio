"""Booking purposes and the suggested accounts in SKR 03 and SKR 04.

These are suggestions for an Einnahmenüberschussrechnung with Istversteuerung; revenue accounts are
assumed to be DATEV "Automatikkonten" (VAT is derived from the account, no BU key). The tax advisor
decides – every account can be overridden in the settings.
"""

from __future__ import annotations

from dataclasses import dataclass

from skontio.accounting.models import AccountAssignment, LedgerSettings
from skontio.expenses.models import Expense


@dataclass(frozen=True)
class Purpose:
    key: str
    label: str
    skr03: str
    skr04: str


MONEY = [
    Purpose("bank", "Bank", "1200", "1800"),
    Purpose("cash", "Kasse", "1000", "1600"),
    Purpose("transit", "Geldtransit (PayPal u. Ä.)", "1360", "1460"),
    Purpose("private", "Privateinlage (privat bezahlte Ausgaben)", "1890", "2180"),
]
INCOME = [
    Purpose("income_small_business", "Erlöse als Kleinunternehmer (§ 19 UStG)", "8195", "4185"),
    Purpose("income_standard", "Erlöse 19 % USt", "8400", "4400"),
    Purpose("income_reduced", "Erlöse 7 % USt", "8300", "4300"),
    Purpose("income_tax_free", "Steuerfreie Erlöse (§ 4 UStG, Nullsatz)", "8100", "4100"),
    Purpose("income_intra_eu", "Steuerfreie innergemeinschaftliche Lieferungen", "8125", "4125"),
    Purpose("income_export", "Steuerfreie Ausfuhrlieferungen", "8120", "4120"),
    Purpose("income_reverse_charge", "Erlöse, Leistungsempfänger schuldet die Steuer (EU, § 13b)", "8336", "4336"),
    Purpose("income_not_taxable", "Im Inland nicht steuerbare Erlöse", "8338", "4338"),
]
C = Expense.Category
EXPENSES = [
    Purpose(f"expense_{C.GOODS}", C.GOODS.label, "3400", "5400"),
    Purpose(f"expense_{C.SUBCONTRACTING}", C.SUBCONTRACTING.label, "3100", "5900"),
    Purpose(f"expense_{C.RENT}", C.RENT.label, "4210", "6310"),
    Purpose(f"expense_{C.TELECOM}", C.TELECOM.label, "4920", "6805"),
    Purpose(f"expense_{C.SOFTWARE}", C.SOFTWARE.label, "4806", "6495"),
    Purpose(f"expense_{C.GWG}", C.GWG.label, "4855", "6260"),
    Purpose(f"expense_{C.OFFICE}", C.OFFICE.label, "4930", "6815"),
    Purpose(f"expense_{C.TRAVEL}", C.TRAVEL.label, "4670", "6670"),
    Purpose(f"expense_{C.VEHICLE}", C.VEHICLE.label, "4500", "6500"),
    Purpose(f"expense_{C.TRAINING}", C.TRAINING.label, "4945", "6821"),
    Purpose(f"expense_{C.ADVERTISING}", C.ADVERTISING.label, "4600", "6600"),
    Purpose(f"expense_{C.INSURANCE}", C.INSURANCE.label, "4360", "6400"),
    Purpose(f"expense_{C.ADVISORY}", C.ADVISORY.label, "4950", "6825"),
    Purpose(f"expense_{C.FEES}", C.FEES.label, "4970", "6855"),
    Purpose(f"expense_{C.HOSPITALITY}", C.HOSPITALITY.label, "4650", "6640"),
    Purpose(f"expense_{C.OTHER}", C.OTHER.label, "4900", "6300"),
]
PURPOSES = {p.key: p for p in MONEY + INCOME + EXPENSES}
assert {f"expense_{c}" for c in C.values} <= set(PURPOSES), "every expense category needs an account"


class AccountError(ValueError):
    pass


def ledger_settings(company) -> LedgerSettings:
    found = LedgerSettings.objects.filter(company=company).first()
    return found or LedgerSettings(company=company)


def suggested(chart: str, key: str) -> str:
    purpose = PURPOSES[key]
    return purpose.skr04 if chart == LedgerSettings.Chart.SKR04 else purpose.skr03


def chart_of_accounts(company) -> dict[str, str]:
    """Account per purpose: own assignment, else the chart's suggestion."""
    chart = ledger_settings(company).chart
    own = dict(AccountAssignment.objects.filter(company=company).values_list("key", "account"))
    return {key: own.get(key) or suggested(chart, key) for key in PURPOSES}


def validate_account(value: str, length: int) -> str:
    value = (value or "").strip()
    if not value.isascii() or not value.isdigit():
        raise AccountError(f"Konto {value[:20]!r}: nur Ziffern.")
    if len(value) != length:
        raise AccountError(f"Konto {value}: muss genau {length} Stellen haben (Sachkontenlänge).")
    return value
