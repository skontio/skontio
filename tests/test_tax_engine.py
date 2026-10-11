"""Steuer-Engine: abstrakte Steuerbehandlung → EN-16931-Kategorie/Satz und Summen nach BR-CO."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from skontio.tax.engine import LineInput, Regime, TaxError, TaxTreatment, calculate, resolve

D = Decimal
TODAY = dt.date(2026, 10, 11)


class TestResolve:
    @pytest.mark.parametrize(
        ("treatment", "category", "rate"),
        [
            (TaxTreatment.STANDARD, "S", D("19")),
            (TaxTreatment.REDUCED, "S", D("7")),
            (TaxTreatment.ZERO, "Z", D("0")),
            (TaxTreatment.EXEMPT, "E", D("0")),
            (TaxTreatment.REVERSE_CHARGE, "AE", D("0")),
            (TaxTreatment.INTRA_EU_SUPPLY, "K", D("0")),
            (TaxTreatment.EXPORT, "G", D("0")),
            (TaxTreatment.NOT_TAXABLE, "O", D("0")),
        ],
    )
    def test_standard_regime(self, treatment, category, rate):
        resolved = resolve(treatment, Regime.STANDARD, TODAY)
        assert (resolved.category, resolved.rate) == (category, rate)

    @pytest.mark.parametrize(
        "treatment",
        [TaxTreatment.STANDARD, TaxTreatment.REDUCED, TaxTreatment.ZERO, TaxTreatment.EXEMPT],
    )
    def test_small_business_never_shows_vat(self, treatment):
        resolved = resolve(treatment, Regime.SMALL_BUSINESS, TODAY)
        assert (resolved.category, resolved.rate) == ("E", D("0"))
        assert "§ 19 UStG" in resolved.exemption_reason

    def test_small_business_not_taxable_stays_out_of_scope(self):
        assert resolve(TaxTreatment.NOT_TAXABLE, Regime.SMALL_BUSINESS, TODAY).category == "O"

    @pytest.mark.parametrize("treatment", [TaxTreatment.REVERSE_CHARGE, TaxTreatment.INTRA_EU_SUPPLY])
    def test_small_business_cannot_use_eu_b2b_schemes(self, treatment):
        with pytest.raises(TaxError):
            resolve(treatment, Regime.SMALL_BUSINESS, TODAY)

    def test_rates_follow_service_date(self):
        # temporary reduction 01.07.–31.12.2020 (Zweites Corona-Steuerhilfegesetz)
        assert resolve(TaxTreatment.STANDARD, Regime.STANDARD, dt.date(2020, 8, 1)).rate == D("16")
        assert resolve(TaxTreatment.REDUCED, Regime.STANDARD, dt.date(2020, 8, 1)).rate == D("5")
        assert resolve(TaxTreatment.STANDARD, Regime.STANDARD, dt.date(2021, 1, 1)).rate == D("19")

    def test_exempt_reasons_are_present_where_en16931_requires_them(self):
        for treatment in (
            TaxTreatment.EXEMPT,
            TaxTreatment.REVERSE_CHARGE,
            TaxTreatment.INTRA_EU_SUPPLY,
            TaxTreatment.EXPORT,
            TaxTreatment.NOT_TAXABLE,
        ):
            assert resolve(treatment, Regime.STANDARD, TODAY).exemption_reason


def line(qty: str, price: str, treatment=TaxTreatment.STANDARD, discount: str = "0", regime=Regime.STANDARD):
    return LineInput(
        quantity=D(qty), unit_price=D(price), discount_percent=D(discount), tax=resolve(treatment, regime, TODAY)
    )


class TestCalculate:
    def test_single_rate(self):
        totals = calculate([line("3", "80.00"), line("1", "249.99")])
        assert totals.line_total == D("489.99")
        assert totals.tax_total == D("93.10")  # 489.99 * 19 % = 93.0981
        assert totals.grand_total == D("583.09")

    def test_vat_is_computed_per_category_not_per_line(self):
        # BR-CO-17 uses the category basis, not the sum of rounded per-line taxes
        totals = calculate([line("1", "0.33"), line("1", "0.33"), line("1", "0.33")])
        assert totals.line_total == D("0.99")
        assert totals.tax_total == D("0.19")

    def test_mixed_rates_produce_one_breakdown_per_rate(self):
        totals = calculate([line("1", "100"), line("2", "10", TaxTreatment.REDUCED)])
        breakdown = {(b.category, b.rate): (b.basis, b.tax) for b in totals.breakdown}
        assert breakdown == {("S", D("19")): (D("100.00"), D("19.00")), ("S", D("7")): (D("20.00"), D("1.40"))}
        assert totals.grand_total == D("140.40")

    def test_line_discount(self):
        totals = calculate([line("2", "50", discount="10")])
        assert totals.lines[0].net_amount == D("90.00")
        assert totals.line_total == D("90.00")

    def test_document_discount_is_split_per_category(self):
        totals = calculate(
            [line("1", "100"), line("1", "100", TaxTreatment.REDUCED)], document_discount_percent=D("10")
        )
        assert totals.allowance_total == D("20.00")
        breakdown = {b.rate: (b.basis, b.tax) for b in totals.breakdown}
        assert breakdown == {D("19"): (D("90.00"), D("17.10")), D("7"): (D("90.00"), D("6.30"))}
        assert totals.tax_basis_total == D("180.00")
        assert totals.grand_total == D("203.40")

    def test_small_business_invoice_has_no_tax(self):
        totals = calculate([line("3", "80", regime=Regime.SMALL_BUSINESS)])
        assert totals.tax_total == D("0.00")
        assert totals.grand_total == D("240.00")

    def test_rejects_negative_quantities_and_invalid_discounts(self):
        with pytest.raises(TaxError):
            calculate([line("-1", "10")])
        with pytest.raises(TaxError):
            calculate([line("1", "10", discount="101")])
        with pytest.raises(TaxError):
            calculate([line("1", "10")], document_discount_percent=D("-5"))

    def test_empty_document_is_rejected(self):
        with pytest.raises(TaxError):
            calculate([])


amounts = st.decimals(min_value=D("0"), max_value=D("99999.9999"), places=4)
quantities = st.decimals(min_value=D("0"), max_value=D("9999.999"), places=3)
percents = st.decimals(min_value=D("0"), max_value=D("100"), places=2)
treatments = st.sampled_from([TaxTreatment.STANDARD, TaxTreatment.REDUCED, TaxTreatment.ZERO, TaxTreatment.EXEMPT])


@given(
    st.lists(st.tuples(quantities, amounts, percents, treatments), min_size=1, max_size=12),
    percents,
)
def test_invariants(raw_lines, document_discount):
    inputs = [
        LineInput(quantity=q, unit_price=p, discount_percent=d, tax=resolve(t, Regime.STANDARD, TODAY))
        for q, p, d, t in raw_lines
    ]
    totals = calculate(inputs, document_discount_percent=document_discount)
    two_places = D("0.01")
    for value in (totals.line_total, totals.allowance_total, totals.tax_basis_total, totals.tax_total):
        assert value == value.quantize(two_places)
        assert value >= 0
    assert totals.line_total == sum((ln.net_amount for ln in totals.lines), D("0"))  # BR-CO-10
    assert totals.tax_basis_total == totals.line_total - totals.allowance_total  # BR-CO-13
    assert totals.tax_total == sum((b.tax for b in totals.breakdown), D("0"))  # BR-CO-14
    assert totals.grand_total == totals.tax_basis_total + totals.tax_total  # BR-CO-15
    assert sum((b.basis for b in totals.breakdown), D("0")) == totals.tax_basis_total
    for b in totals.breakdown:
        assert b.tax == (b.basis * b.rate / 100).quantize(two_places, rounding="ROUND_HALF_UP")  # BR-CO-17
