"""VAT regime periods per company (§ 19 UStG).

Rules encoded here (see docs/legal/sources.md):
* Exceeding the previous-year limit, the voluntary option (§ 19 Abs. 3) and returning to the
  small-business scheme take effect at the start of a calendar year.
* Exceeding the current-year limit takes effect on the day it happens.
* The voluntary option binds for at least five calendar years.
Skontio never switches automatically; a person confirms every change.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction

from skontio.tax.engine import Regime
from skontio.taxstatus.models import TaxStatusPeriod

Reason = TaxStatusPeriod.Reason

_YEAR_START_REASONS = {Reason.THRESHOLD_PREVIOUS_YEAR, Reason.VOLUNTARY_OPTION, Reason.RETURN_TO_SMALL_BUSINESS}
_REASONS_BY_TARGET = {
    Regime.STANDARD: {Reason.THRESHOLD_PREVIOUS_YEAR, Reason.THRESHOLD_CURRENT_YEAR, Reason.VOLUNTARY_OPTION},
    Regime.SMALL_BUSINESS: {Reason.RETURN_TO_SMALL_BUSINESS},
}
OPTION_BINDING_YEARS = 5


class TaxStatusError(ValueError):
    pass


def start_regime(company, regime: Regime, valid_from: dt.date, *, confirmed_by=None) -> TaxStatusPeriod:
    if TaxStatusPeriod.objects.filter(company=company).exists():
        raise TaxStatusError("Der Steuerstatus ist bereits eingerichtet; Änderungen nur über einen Wechsel")
    return TaxStatusPeriod.objects.create(
        company=company, regime=regime, valid_from=valid_from, reason=Reason.INITIAL, confirmed_by=confirmed_by
    )


def regime_for(company, on_date: dt.date) -> Regime:
    period = TaxStatusPeriod.objects.filter(company=company, valid_from__lte=on_date).order_by("-valid_from").first()
    if period is None or (period.valid_to is not None and period.valid_to < on_date):
        raise TaxStatusError(f"Für {on_date:%d.%m.%Y} ist kein Steuerstatus hinterlegt")
    return Regime(period.regime)


def change_regime(
    company, target: Regime, valid_from: dt.date, reason: Reason, *, confirmed_by=None
) -> TaxStatusPeriod:
    if reason not in _REASONS_BY_TARGET[target]:
        raise TaxStatusError(f"Der Grund {reason.label!r} passt nicht zum Wechsel nach {target}")
    if reason in _YEAR_START_REASONS and (valid_from.month, valid_from.day) != (1, 1):
        raise TaxStatusError(f"{reason.label} wirkt nur ab dem 1. Januar eines Jahres")

    with transaction.atomic():
        current = TaxStatusPeriod.objects.select_for_update().filter(company=company, valid_to__isnull=True).first()
        if current is None:
            raise TaxStatusError("Es ist noch kein Steuerstatus eingerichtet")
        if current.regime == target:
            raise TaxStatusError(f"Der Steuerstatus ist bereits {target}")
        if valid_from <= current.valid_from:
            raise TaxStatusError("Der Stichtag muss nach dem Beginn des aktuellen Zeitraums liegen")
        if current.option_binding_until and valid_from <= current.option_binding_until:
            raise TaxStatusError(
                f"Der Verzicht auf die Kleinunternehmerregelung bindet bis {current.option_binding_until:%d.%m.%Y}"
            )

        current.valid_to = valid_from - dt.timedelta(days=1)
        current.save(update_fields=["valid_to"])
        binding = (
            dt.date(valid_from.year + OPTION_BINDING_YEARS - 1, 12, 31) if reason is Reason.VOLUNTARY_OPTION else None
        )
        return TaxStatusPeriod.objects.create(
            company=company,
            regime=target,
            valid_from=valid_from,
            reason=reason,
            option_binding_until=binding,
            confirmed_by=confirmed_by,
        )
