"""Zahlungseingänge und Kleinunternehmer-Grenzen (§ 19 UStG, vereinnahmte Entgelte)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, cancel_invoice, create_draft, finalize
from skontio.payments.models import Payment
from skontio.payments.services import PaymentError, open_amount, payment_status, record_payment, reverse_payment
from skontio.tax.engine import Regime
from skontio.taxstatus.models import TaxStatusPeriod
from skontio.taxstatus.services import change_regime, start_regime
from skontio.taxstatus.thresholds import Level, small_business_status, would_exceed

D = Decimal


@pytest.fixture
def company(db):
    company = Company.objects.create(
        name="Muster",
        street="Weg 1",
        postcode="12345",
        city="Stadt",
        tax_number="123/456/78901",
        iban="DE02120300000000202051",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def customer(company):
    return create_customer(company, name="Kunde GmbH", street="Weg 5", postcode="54321", city="Köln")


def invoice(company, customer, amount: str, *, service_date=dt.date(2026, 3, 1)) -> Document:
    doc = create_draft(company, Document.Type.INVOICE, customer, issue_date=service_date, service_date=service_date)
    item = create_item(company, name=f"Leistung {amount}", unit="LS", net_price=D(amount))
    add_line_from_item(doc, item, quantity=D("1"))
    return finalize(doc)


@pytest.mark.django_db
class TestPayments:
    def test_partial_and_full_payment(self, company, customer):
        doc = invoice(company, customer, "1000")
        assert payment_status(doc) == "open"
        record_payment(doc, D("400"), dt.date(2026, 3, 10))
        assert (open_amount(doc), payment_status(doc)) == (D("600.00"), "partial")
        record_payment(doc, D("600"), dt.date(2026, 3, 20))
        assert (open_amount(doc), payment_status(doc)) == (D("0.00"), "paid")

    def test_overpayment_is_rejected(self, company, customer):
        doc = invoice(company, customer, "100")
        with pytest.raises(PaymentError):
            record_payment(doc, D("100.01"), dt.date(2026, 3, 10))

    def test_payments_only_on_finalized_invoices(self, company, customer):
        draft = create_draft(company, Document.Type.INVOICE, customer, issue_date=dt.date(2026, 3, 1))
        with pytest.raises(PaymentError):
            record_payment(draft, D("1"), dt.date(2026, 3, 1))

    def test_no_payments_on_cancelled_invoices(self, company, customer):
        doc = invoice(company, customer, "100")
        cancel_invoice(doc, issue_date=dt.date(2026, 3, 2), reason="Fehler")
        with pytest.raises(PaymentError):
            record_payment(doc, D("100"), dt.date(2026, 3, 10))

    def test_amount_must_be_positive(self, company, customer):
        doc = invoice(company, customer, "100")
        with pytest.raises(PaymentError):
            record_payment(doc, D("0"), dt.date(2026, 3, 10))

    def test_payments_are_reversed_not_deleted(self, company, customer):
        doc = invoice(company, customer, "100")
        payment = record_payment(doc, D("100"), dt.date(2026, 3, 10))
        reversal = reverse_payment(payment, reason="Falsch zugeordnet", on=dt.date(2026, 3, 11))
        assert reversal.amount == D("-100.00")
        assert reversal.reverses_id == payment.pk
        assert Payment.objects.filter(pk=payment.pk).exists()
        assert payment_status(doc) == "open"
        with pytest.raises(PaymentError):
            reverse_payment(payment, reason="nochmal", on=dt.date(2026, 3, 12))
        with pytest.raises(PaymentError):
            payment.delete()


@pytest.mark.django_db
class TestSmallBusinessThresholds:
    def test_revenue_counts_received_payments_of_the_calendar_year(self, company, customer):
        late = invoice(company, customer, "3000", service_date=dt.date(2025, 12, 15))
        record_payment(invoice(company, customer, "5000"), D("5000"), dt.date(2026, 2, 1))
        record_payment(late, D("3000"), dt.date(2026, 1, 5))  # Zufluss 2026 counts for 2026
        unpaid = invoice(company, customer, "9000")  # issued, not received – does not count
        assert unpaid.gross_total == D("9000.00")
        status = small_business_status(company, dt.date(2026, 6, 30))
        assert status.revenue == D("8000.00")
        assert status.current_limit == D("100000")
        assert status.level is Level.OK

    def test_previous_year_above_25000_means_standard_regime_next_year(self, company, customer):
        doc = invoice(company, customer, "26000", service_date=dt.date(2025, 5, 1))
        record_payment(doc, D("26000"), dt.date(2025, 5, 2))
        status = small_business_status(company, dt.date(2026, 1, 10))
        assert status.previous_year_revenue == D("26000.00")
        assert status.previous_year_exceeded is True

    @pytest.mark.parametrize(
        ("received", "level"),
        [
            ("79999", Level.OK),
            ("80000", Level.WARN_80),
            ("90000", Level.WARN_90),
            ("100000", Level.WARN_90),
            ("100000.01", Level.EXCEEDED),
        ],
    )
    def test_warning_levels(self, company, customer, received, level):
        record_payment(invoice(company, customer, received), D(received), dt.date(2026, 4, 1))
        assert small_business_status(company, dt.date(2026, 4, 2)).level is level

    def test_crossing_date_is_the_payment_that_exceeds(self, company, customer):
        record_payment(invoice(company, customer, "60000"), D("60000"), dt.date(2026, 4, 1))
        record_payment(invoice(company, customer, "45000"), D("45000"), dt.date(2026, 7, 15))
        status = small_business_status(company, dt.date(2026, 8, 1))
        assert status.level is Level.EXCEEDED
        assert status.crossed_on == dt.date(2026, 7, 15)

    def test_would_exceed_preview(self, company, customer):
        record_payment(invoice(company, customer, "95000"), D("95000"), dt.date(2026, 4, 1))
        assert would_exceed(company, D("4000"), dt.date(2026, 5, 1)) is False
        assert would_exceed(company, D("6000"), dt.date(2026, 5, 1)) is True

    def test_standard_regime_counts_net_share_of_payments(self, company, customer):
        change_regime(company, Regime.STANDARD, dt.date(2026, 1, 1), TaxStatusPeriod.Reason.VOLUNTARY_OPTION)
        doc = invoice(company, customer, "1000")  # 1190 gross
        record_payment(doc, D("1190"), dt.date(2026, 3, 10))
        assert small_business_status(company, dt.date(2026, 4, 1)).revenue == D("1000.00")

    def test_reversals_reduce_revenue(self, company, customer):
        payment = record_payment(invoice(company, customer, "5000"), D("5000"), dt.date(2026, 2, 1))
        reverse_payment(payment, reason="Rücklastschrift", on=dt.date(2026, 2, 5))
        assert small_business_status(company, dt.date(2026, 3, 1)).revenue == D("0.00")
