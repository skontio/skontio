"""Steuerstatus: Kleinunternehmer (§ 19 UStG) ↔ Regelbesteuerung mit Stichtag."""

from __future__ import annotations

import datetime as dt

import pytest

from skontio.core.models import Company
from skontio.tax.engine import Regime
from skontio.taxstatus.models import TaxStatusPeriod
from skontio.taxstatus.services import TaxStatusError, change_regime, regime_for, start_regime

Reason = TaxStatusPeriod.Reason


@pytest.fixture
def company(db):
    company = Company.objects.create(name="Nebengewerbe Muster")
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 3, 1))
    return company


@pytest.mark.django_db
class TestRegimeFor:
    def test_initial_regime(self, company):
        assert regime_for(company, dt.date(2026, 10, 11)) is Regime.SMALL_BUSINESS

    def test_date_before_first_period_is_an_error(self, company):
        with pytest.raises(TaxStatusError):
            regime_for(company, dt.date(2024, 2, 29))

    def test_start_regime_only_once(self, company):
        with pytest.raises(TaxStatusError):
            start_regime(company, Regime.STANDARD, dt.date(2024, 1, 1))


@pytest.mark.django_db
class TestChangeRegime:
    def test_current_year_threshold_switches_on_any_day(self, company):
        change_regime(company, Regime.STANDARD, dt.date(2026, 8, 17), Reason.THRESHOLD_CURRENT_YEAR)
        assert regime_for(company, dt.date(2026, 8, 16)) is Regime.SMALL_BUSINESS
        assert regime_for(company, dt.date(2026, 8, 17)) is Regime.STANDARD
        previous = TaxStatusPeriod.objects.get(company=company, regime=Regime.SMALL_BUSINESS)
        assert previous.valid_to == dt.date(2026, 8, 16)

    @pytest.mark.parametrize("reason", [Reason.THRESHOLD_PREVIOUS_YEAR, Reason.VOLUNTARY_OPTION])
    def test_year_based_changes_only_on_january_first(self, company, reason):
        with pytest.raises(TaxStatusError, match="1. Januar"):
            change_regime(company, Regime.STANDARD, dt.date(2027, 3, 1), reason)
        change_regime(company, Regime.STANDARD, dt.date(2027, 1, 1), reason)
        assert regime_for(company, dt.date(2027, 1, 1)) is Regime.STANDARD

    def test_voluntary_option_binds_five_calendar_years(self, company):
        change_regime(company, Regime.STANDARD, dt.date(2027, 1, 1), Reason.VOLUNTARY_OPTION)
        period = TaxStatusPeriod.objects.get(company=company, regime=Regime.STANDARD)
        assert period.option_binding_until == dt.date(2031, 12, 31)
        with pytest.raises(TaxStatusError, match="bindet"):
            change_regime(company, Regime.SMALL_BUSINESS, dt.date(2031, 1, 1), Reason.RETURN_TO_SMALL_BUSINESS)
        change_regime(company, Regime.SMALL_BUSINESS, dt.date(2032, 1, 1), Reason.RETURN_TO_SMALL_BUSINESS)
        assert regime_for(company, dt.date(2032, 1, 1)) is Regime.SMALL_BUSINESS

    def test_return_to_small_business_only_on_january_first(self, company):
        change_regime(company, Regime.STANDARD, dt.date(2026, 8, 17), Reason.THRESHOLD_CURRENT_YEAR)
        with pytest.raises(TaxStatusError, match="1. Januar"):
            change_regime(company, Regime.SMALL_BUSINESS, dt.date(2027, 6, 1), Reason.RETURN_TO_SMALL_BUSINESS)

    def test_change_must_be_after_current_period_start(self, company):
        with pytest.raises(TaxStatusError):
            change_regime(company, Regime.STANDARD, dt.date(2024, 3, 1), Reason.THRESHOLD_CURRENT_YEAR)

    def test_change_to_same_regime_is_rejected(self, company):
        with pytest.raises(TaxStatusError):
            change_regime(company, Regime.SMALL_BUSINESS, dt.date(2027, 1, 1), Reason.RETURN_TO_SMALL_BUSINESS)

    def test_reason_must_fit_direction(self, company):
        with pytest.raises(TaxStatusError):
            change_regime(company, Regime.STANDARD, dt.date(2027, 1, 1), Reason.RETURN_TO_SMALL_BUSINESS)

    def test_change_records_who_confirmed(self, company, django_user_model):
        user = django_user_model.objects.create_user("inhaber")
        change_regime(company, Regime.STANDARD, dt.date(2026, 8, 17), Reason.THRESHOLD_CURRENT_YEAR, confirmed_by=user)
        assert TaxStatusPeriod.objects.get(company=company, regime=Regime.STANDARD).confirmed_by == user
