"""Worker-Container: tägliche Aufgaben einmal pro Tag, Nachholen, Wiederholen bei Fehlern, Doctor-Prüfung."""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from django.utils import timezone

from skontio.core.doctor import check_worker
from skontio.core.models import JobRun
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.ops import scheduler
from skontio.recurring.services import create_from_invoice


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return timezone.make_aware(dt.datetime(2026, 10, day, hour, minute))


@pytest.fixture
def calls(monkeypatch, settings):
    settings.WORKER_BACKUP_AT, settings.WORKER_RECURRING_AT = "02:30", "off"
    seen = []

    def fake_backup(*args, **kwargs):
        seen.append(kwargs.get("reason"))
        kwargs["stdout"].write("Backup erstellt: /backups/x.tar\n")

    monkeypatch.setattr(scheduler, "call_command", fake_backup)
    return seen


class TestSchedule:
    def test_runs_once_per_day_at_or_after_its_time(self, db, calls):
        assert scheduler.run_due(at(13, 2, 0)) == []
        (run,) = scheduler.run_due(at(13, 2, 30))
        assert run.status == JobRun.Status.OK and run.message == "Backup erstellt: /backups/x.tar"
        assert scheduler.run_due(at(13, 23, 0)) == []
        assert len(scheduler.run_due(at(14, 3, 0))) == 1
        assert calls == ["nightly", "nightly"]

    def test_catches_up_when_started_later(self, db, calls):
        assert len(scheduler.run_due(at(13, 15, 0))) == 1

    def test_failures_are_retried_hourly_at_most_three_times(self, db, monkeypatch, settings):
        settings.WORKER_BACKUP_AT, settings.WORKER_RECURRING_AT = "02:30", "off"

        def broken(*args, **kwargs):
            raise OSError("Festplatte voll")

        monkeypatch.setattr(scheduler, "call_command", broken)
        (run,) = scheduler.run_due(at(13, 2, 30))
        assert run.status == JobRun.Status.FAILED and "Festplatte voll" in run.message
        assert scheduler.run_due(at(13, 3, 0)) == []  # not yet an hour later
        assert scheduler.run_due(at(13, 3, 31))[0].attempts == 2
        assert scheduler.run_due(at(13, 4, 32))[0].attempts == 3
        assert scheduler.run_due(at(13, 9, 0)) == []
        assert JobRun.objects.get().attempts == 3

    def test_disabled_jobs_and_invalid_times(self, settings):
        settings.WORKER_BACKUP_AT, settings.WORKER_RECURRING_AT = "off", ""
        assert scheduler.jobs() == []
        settings.WORKER_BACKUP_AT = "2 Uhr"
        with pytest.raises(ValueError, match="HH:MM"):
            scheduler.jobs()

    def test_recurring_invoices_become_drafts(self, shop, settings):
        settings.WORKER_BACKUP_AT, settings.WORKER_RECURRING_AT = "off", "06:00"
        doc = create_draft(
            shop.company,
            Document.Type.INVOICE,
            shop.org,
            issue_date=dt.date(2026, 9, 1),
            service_date=dt.date(2026, 9, 1),
        )
        add_line_from_item(doc, shop.item, quantity=Decimal("1"))
        create_from_invoice(finalize(doc), name="Wartung", interval=1, start_date=dt.date(2026, 10, 1))
        (run,) = scheduler.run_due(timezone.now())
        assert run.status == JobRun.Status.OK and run.message.startswith("1 Entwurf")
        assert Document.objects.filter(status=Document.Status.DRAFT, origin=Document.Origin.RECURRING).count() == 1


class TestLoop:
    def test_waits_for_migrations_and_stops_on_signal(self, db, monkeypatch):
        stop, ticks = threading.Event(), []
        monkeypatch.setattr(scheduler, "migrations_pending", lambda: True)

        def never(*args, **kwargs):
            raise AssertionError("darf vor den Migrationen nicht laufen")

        def wait(timeout):
            ticks.append(timeout)
            stop.set()

        monkeypatch.setattr(scheduler, "run_due", never)
        monkeypatch.setattr(stop, "wait", wait)
        scheduler.serve(stop, tick=5)
        assert ticks == [5]

    def test_migrations_are_applied_in_tests(self, db):
        assert scheduler.migrations_pending() is False

    def test_entrypoint_starts_the_worker(self):
        entrypoint = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
        assert "exec skontio worker" in entrypoint.read_text()


def test_doctor_reports_stale_and_failed_jobs(db, settings):
    settings.WORKER_BACKUP_AT, settings.WORKER_RECURRING_AT = "02:30", "06:00"
    today = dt.date(2026, 10, 13)
    JobRun.objects.create(job="backup", day=today, status="ok", started_at=at(13, 2), message="Backup erstellt")
    JobRun.objects.create(job="recurring", day=dt.date(2026, 10, 10), status="ok", started_at=at(10, 6))
    backup, recurring = check_worker(scheduler.status(), today=today)
    assert backup.ok and not recurring.ok and "10.10.2026" in recurring.detail
    JobRun.objects.all().delete()
    assert [check.ok for check in check_worker(scheduler.status(), today=today)] == [False, False]


def test_package_version_matches_pyproject():
    """The release workflow compares the git tag with pyproject.toml; backups and MCP report __version__."""
    import tomllib

    from skontio import __version__

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text())["project"]["version"] == __version__
