"""Änderungsprotokoll: hashverkettet, UPDATE gesperrt, Löschen und Fälschen werden erkannt."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, InternalError, OperationalError, connection, transaction

from skontio.audit.models import AuditEvent
from skontio.audit.services import digest, record, verify_chain

DB_ERRORS = (IntegrityError, InternalError, OperationalError)


@pytest.fixture
def events(db):
    return [record("document.finalize", entity="document", entity_id=i, data={"n": i}) for i in range(1, 5)]


def test_chain_links_each_event_to_its_predecessor(events):
    assert [e.seq for e in events] == [1, 2, 3, 4]
    assert events[0].prev_hash == "0" * 64
    assert all(b.prev_hash == a.hash for a, b in zip(events, events[1:], strict=False))
    assert verify_chain() == []
    call_command("verify_audit")


def test_ai_actions_are_marked(db):
    event = record("draft.create", actor_kind=AuditEvent.ActorKind.MCP, client_id="claude", tool_name="x")
    assert event.ai_initiated
    assert not record("draft.create").ai_initiated


def test_update_is_refused_by_the_database(events):
    with pytest.raises(DB_ERRORS), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE audit_auditevent SET action = 'harmlos' WHERE seq = 2")


def test_deleted_event_is_detected(events):
    AuditEvent.objects.filter(seq=2).delete()
    problems = verify_chain()
    assert any("fehlen" in p for p in problems)
    with pytest.raises(CommandError):
        call_command("verify_audit")


def test_cut_off_end_is_detected(events):
    AuditEvent.objects.filter(seq=4).delete()
    assert any("Ende der Kette" in p for p in verify_chain())


def test_forged_event_is_detected(events):
    original = AuditEvent.objects.get(seq=3)
    AuditEvent.objects.filter(seq=3).delete()
    original.pk, original.data = None, {"n": 999}
    original.save()  # re-inserted with the old hash but other content
    assert "#3: Inhalt wurde verändert" in verify_chain()


def test_digest_is_stable_and_hides_content():
    assert digest({"b": 1, "a": "Kunde Müller"}) == digest({"a": "Kunde Müller", "b": 1})
    assert "Müller" not in digest({"a": "Kunde Müller"})


def test_rollback_also_removes_the_event(db):
    with pytest.raises(RuntimeError), transaction.atomic():
        record("draft.create")
        raise RuntimeError
    assert verify_chain() == []
    assert record("draft.create").seq == 1
