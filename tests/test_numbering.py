"""Gap-free, unique document numbers (§ 14 Abs. 4 Nr. 4 UStG: fortlaufend, einmalig)."""

from __future__ import annotations

import datetime as dt
import threading

import pytest
from django.db import connection, transaction

from skontio.core.models import Company
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import NumberingError, allocate_number, format_number


class TestFormatNumber:
    @pytest.mark.parametrize(
        ("pattern", "value", "date", "expected"),
        [
            ("RE-{YYYY}-{####}", 7, dt.date(2026, 3, 1), "RE-2026-0007"),
            ("{YY}{MM}-{###}", 12, dt.date(2026, 11, 5), "2611-012"),
            ("AN{#}", 12345, dt.date(2026, 1, 1), "AN12345"),
            ("RE-{####}", 12345, dt.date(2026, 1, 1), "RE-12345"),  # width is a minimum, never truncates
        ],
    )
    def test_formats_tokens(self, pattern, value, date, expected):
        assert format_number(pattern, value, date) == expected

    @pytest.mark.parametrize("pattern", ["RE-{YYYY}", "{#}-{#}", "RE-{YYYY}-{####", ""])
    def test_rejects_patterns_without_exactly_one_counter(self, pattern):
        with pytest.raises(NumberingError):
            format_number(pattern, 1, dt.date(2026, 1, 1))


@pytest.fixture
def company(db):
    return Company.objects.create(name="Testfirma")


@pytest.fixture
def invoice_sequence(company):
    return NumberSequence.objects.create(
        company=company, doc_type="invoice", pattern="RE-{YYYY}-{####}", reset=NumberSequence.Reset.YEARLY
    )


@pytest.mark.django_db
class TestAllocate:
    def test_numbers_are_consecutive(self, company, invoice_sequence):
        day = dt.date(2026, 5, 1)
        with transaction.atomic():
            numbers = [allocate_number(company, "invoice", day) for _ in range(3)]
        assert numbers == ["RE-2026-0001", "RE-2026-0002", "RE-2026-0003"]

    def test_yearly_reset(self, company, invoice_sequence):
        with transaction.atomic():
            allocate_number(company, "invoice", dt.date(2026, 12, 31))
            assert allocate_number(company, "invoice", dt.date(2027, 1, 1)) == "RE-2027-0001"

    def test_never_reset_keeps_counting(self, company):
        NumberSequence.objects.create(company=company, doc_type="quote", pattern="AN-{#####}", reset="never")
        with transaction.atomic():
            allocate_number(company, "quote", dt.date(2026, 12, 31))
            assert allocate_number(company, "quote", dt.date(2027, 1, 1)) == "AN-00002"

    def test_backdated_allocation_into_closed_year_is_refused(self, company, invoice_sequence):
        with transaction.atomic():
            allocate_number(company, "invoice", dt.date(2027, 1, 2))
        with pytest.raises(NumberingError), transaction.atomic():
            allocate_number(company, "invoice", dt.date(2026, 12, 30))

    def test_rollback_leaves_no_gap(self, company, invoice_sequence):
        day = dt.date(2026, 5, 1)
        with pytest.raises(RuntimeError), transaction.atomic():
            allocate_number(company, "invoice", day)
            raise RuntimeError("Festschreiben fehlgeschlagen")
        with transaction.atomic():
            assert allocate_number(company, "invoice", day) == "RE-2026-0001"

    def test_unknown_sequence(self, company):
        with pytest.raises(NumberingError), transaction.atomic():
            allocate_number(company, "invoice", dt.date(2026, 5, 1))

    def test_sequences_are_isolated_per_company(self, company, invoice_sequence):
        other = Company.objects.create(name="Andere")
        NumberSequence.objects.create(company=other, doc_type="invoice", pattern="RE-{YYYY}-{####}")
        with transaction.atomic():
            allocate_number(company, "invoice", dt.date(2026, 5, 1))
            assert allocate_number(other, "invoice", dt.date(2026, 5, 1)) == "RE-2026-0001"


@pytest.mark.django_db(transaction=True)
def test_allocation_outside_transaction_is_refused():
    # pytest-django wraps plain db tests in a transaction, so this one must be transactional
    company = Company.objects.create(name="Ohne Transaktion")
    NumberSequence.objects.create(company=company, doc_type="invoice", pattern="{#}")
    with pytest.raises(NumberingError):
        allocate_number(company, "invoice", dt.date(2026, 5, 1))


@pytest.mark.django_db(transaction=True)
def test_concurrent_allocation_is_gap_free_and_unique():
    """50 parallel writers must produce exactly 1..50 (SQLite: BEGIN IMMEDIATE, Postgres: FOR UPDATE)."""
    company = Company.objects.create(name="Parallel GmbH")
    NumberSequence.objects.create(company=company, doc_type="invoice", pattern="{#}")
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def worker():
        try:
            start.wait()
            for _ in range(5):
                with transaction.atomic():
                    number = allocate_number(company, "invoice", dt.date(2026, 5, 1))
                with lock:
                    results.append(number)
        except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
            errors.append(exc)
        finally:
            connection.close()  # thread-local connection; CONN_MAX_AGE would keep it open

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(int(n) for n in results) == list(range(1, 51))
