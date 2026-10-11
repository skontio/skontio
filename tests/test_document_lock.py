"""Festschreibung: finalized documents are immutable down to the database (GoBD Unveränderbarkeit)."""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal

import pytest
from django.db import DatabaseError, connection, transaction

from skontio.core.models import Company
from skontio.documents.models import Document, DocumentLine
from skontio.documents.services import DocumentLocked, finalize
from skontio.numbering.models import NumberSequence


@pytest.fixture
def company(db):
    company = Company.objects.create(name="Testfirma")
    NumberSequence.objects.create(company=company, doc_type="invoice", pattern="RE-{YYYY}-{####}")
    return company


def make_draft(company, **kwargs) -> Document:
    doc = Document.objects.create(
        company=company, doc_type="invoice", issue_date=kwargs.pop("issue_date", dt.date(2026, 10, 11)), **kwargs
    )
    DocumentLine.objects.create(
        document=doc, position=1, text="Beratung", quantity=Decimal("3"), unit_price=Decimal("80.00")
    )
    return doc


def raw(sql: str, params: list) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@pytest.mark.django_db
class TestFinalize:
    def test_assigns_number_status_and_timestamp(self, company):
        doc = finalize(make_draft(company))
        assert doc.number == "RE-2026-0001"
        assert doc.status == Document.Status.FINALIZED
        assert doc.locked_at is not None

    def test_drafts_have_no_number(self, company):
        assert make_draft(company).number is None

    def test_cannot_finalize_twice(self, company):
        doc = finalize(make_draft(company))
        with pytest.raises(DocumentLocked):
            finalize(doc)

    def test_cannot_finalize_without_lines(self, company):
        doc = Document.objects.create(company=company, doc_type="invoice", issue_date=dt.date(2026, 10, 11))
        with pytest.raises(ValueError):
            finalize(doc)

    def test_orm_save_of_finalized_document_is_refused(self, company):
        doc = finalize(make_draft(company))
        doc.issue_date = dt.date(2026, 1, 1)
        with pytest.raises(DocumentLocked):
            doc.save()

    def test_orm_delete_of_finalized_document_is_refused(self, company):
        doc = finalize(make_draft(company))
        with pytest.raises(DocumentLocked):
            doc.delete()

    def test_draft_stays_editable_and_deletable(self, company):
        doc = make_draft(company)
        doc.issue_date = dt.date(2026, 1, 1)
        doc.save()
        doc.lines.update(text="Workshop")
        doc.delete()
        assert not Document.objects.exists()


@pytest.mark.django_db
class TestDatabaseTriggers:
    """The ORM can be bypassed; the database itself must refuse changes."""

    def test_raw_update_is_rejected(self, company):
        doc = finalize(make_draft(company))
        with pytest.raises(DatabaseError):
            raw("UPDATE documents_document SET number = %s WHERE id = %s", ["RE-9999", doc.pk])
        doc.refresh_from_db()
        assert doc.number == "RE-2026-0001"

    def test_raw_delete_is_rejected(self, company):
        doc = finalize(make_draft(company))
        with pytest.raises(DatabaseError):
            raw("DELETE FROM documents_document WHERE id = %s", [doc.pk])
        assert Document.objects.filter(pk=doc.pk).exists()

    def test_raw_line_update_is_rejected(self, company):
        doc = finalize(make_draft(company))
        with pytest.raises(DatabaseError):
            raw("UPDATE documents_documentline SET unit_price = %s WHERE document_id = %s", ["1.00", doc.pk])

    def test_raw_line_delete_is_rejected(self, company):
        doc = finalize(make_draft(company))
        with pytest.raises(DatabaseError):
            raw("DELETE FROM documents_documentline WHERE document_id = %s", [doc.pk])

    def test_raw_line_insert_is_rejected(self, company):
        doc = finalize(make_draft(company))
        with pytest.raises(DatabaseError):
            raw(
                "INSERT INTO documents_documentline (document_id, position, text, quantity, unit_price) "
                "VALUES (%s, 2, 'Nachtrag', 1, 10)",
                [doc.pk],
            )

    def test_raw_unlock_via_status_is_rejected(self, company):
        doc = finalize(make_draft(company))
        with pytest.raises(DatabaseError):
            raw("UPDATE documents_document SET status = 'draft' WHERE id = %s", [doc.pk])


@pytest.mark.django_db(transaction=True)
def test_concurrent_finalize_is_gap_free():
    if connection.vendor == "sqlite":
        # Django's test teardown flushes SQLite with DELETE, which the lock triggers refuse.
        # SQLite concurrency is covered by test_numbering (same BEGIN IMMEDIATE transaction).
        pytest.skip("runs on Postgres only (teardown flush vs. lock triggers)")
    company = Company.objects.create(name="Parallel GmbH")
    NumberSequence.objects.create(company=company, doc_type="invoice", pattern="{#}")
    drafts = [make_draft(company) for _ in range(20)]
    errors: list[BaseException] = []
    start = threading.Barrier(len(drafts))

    def worker(pk: int):
        try:
            start.wait()
            finalize(Document.objects.get(pk=pk))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()  # thread-local connection; CONN_MAX_AGE would keep it open

    threads = [threading.Thread(target=worker, args=(d.pk,)) for d in drafts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    numbers = sorted(int(n) for n in Document.objects.values_list("number", flat=True))
    assert numbers == list(range(1, 21))
