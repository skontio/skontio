"""Daily background jobs for the worker container (``skontio worker``).

Each job runs once per local day, at or after its configured time. A worker that starts later –
after an update or a NAS reboot – catches up immediately. ``JobRun`` (unique per job and day)
prevents double runs; a failed job is retried after ``RETRY_AFTER`` up to ``MAX_ATTEMPTS`` times
per day. Jobs only start once the web container has applied all migrations.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from skontio.core.models import Company, JobRun

log = logging.getLogger("skontio.worker")

MAX_ATTEMPTS = 3
RETRY_AFTER = dt.timedelta(hours=1)
TICK_SECONDS = 30


@dataclass(frozen=True)
class Job:
    name: str
    label: str
    at: dt.time
    run: Callable[[], str]


def _backup() -> str:
    output = io.StringIO()
    call_command("backup", reason="nightly", stdout=output)  # CommandError → job failed
    return output.getvalue().strip().splitlines()[0]


def _recurring() -> str:
    from skontio.recurring.services import generate_due

    today = timezone.localdate()
    drafts = [draft for company in Company.objects.all() for draft in generate_due(company, today=today)]
    return f"{len(drafts)} Entwurf/Entwürfe angelegt"


def parse_time(value: str) -> dt.time | None:
    """``"02:30"`` → 02:30; empty or ``off`` disables the job."""
    value = value.strip().lower()
    if value in ("", "off", "aus"):
        return None
    try:
        hours, minutes = value.split(":")
        return dt.time(int(hours), int(minutes))
    except ValueError as exc:
        raise ValueError(f"Ungültige Uhrzeit {value!r} – erwartet HH:MM oder 'off'") from exc


def jobs() -> list[Job]:
    configured = [
        ("backup", "Nächtliches Backup", settings.WORKER_BACKUP_AT, _backup),
        ("recurring", "Wiederkehrende Rechnungen", settings.WORKER_RECURRING_AT, _recurring),
    ]
    return [Job(name, label, at, run) for name, label, value, run in configured if (at := parse_time(value))]


def migrations_pending() -> bool:
    executor = MigrationExecutor(connection)
    return bool(executor.migration_plan(executor.loader.graph.leaf_nodes()))


def _claim(job: Job, now: dt.datetime) -> JobRun | None:
    """Mark the job as running for today, or return None if it is not due."""
    local = timezone.localtime(now)
    if local.time() < job.at:
        return None
    with transaction.atomic():
        row = JobRun.objects.select_for_update().filter(job=job.name, day=local.date()).first()
        if row is None:
            try:
                with transaction.atomic():
                    return JobRun.objects.create(job=job.name, day=local.date(), attempts=1, started_at=now)
            except IntegrityError:  # another worker claimed it in the meantime
                return None
        retry = row.status == JobRun.Status.FAILED and row.attempts < MAX_ATTEMPTS
        if not retry or now - row.started_at < RETRY_AFTER:
            return None
        row.status, row.attempts, row.started_at, row.finished_at = JobRun.Status.RUNNING, row.attempts + 1, now, None
        row.save()
        return row


def run_due(now: dt.datetime | None = None) -> list[JobRun]:
    """Run every job that is due; returns the runs that happened in this call."""
    now = now or timezone.now()
    done = []
    for job in jobs():
        row = _claim(job, now)
        if row is None:
            continue
        log.info("%s gestartet", job.label)
        try:
            message = job.run()
        except Exception as exc:  # noqa: BLE001 – a failing job must not stop the worker
            row.status, row.message = JobRun.Status.FAILED, f"{type(exc).__name__}: {exc}"[:4000]
            log.error("%s fehlgeschlagen (Versuch %s): %s", job.label, row.attempts, row.message)
        else:
            row.status, row.message = JobRun.Status.OK, message
            log.info("%s: %s", job.label, message)
        row.finished_at = timezone.now()
        row.save(update_fields=["status", "message", "finished_at"])
        done.append(row)
    return done


def serve(stop: threading.Event, tick: float = TICK_SECONDS) -> None:
    """Main loop of the worker container until ``stop`` is set (SIGTERM)."""
    names = ", ".join(f"{job.label} {job.at:%H:%M}" for job in jobs()) or "keine Aufgaben"
    log.info("Worker gestartet (%s)", names)
    waiting = False
    while not stop.is_set():
        close_old_connections()
        try:
            if migrations_pending():
                if not waiting:
                    log.info("Warte auf Datenbankmigrationen des Web-Containers …")
                waiting = True
            else:
                waiting = False
                run_due()
        except Exception:  # noqa: BLE001 – e.g. database briefly unreachable; try again next tick
            log.exception("Worker-Durchlauf fehlgeschlagen")
        stop.wait(tick)
    close_old_connections()
    log.info("Worker beendet")


def status() -> list[tuple[Job, JobRun | None]]:
    """Latest run per configured job – for ``skontio doctor worker``."""
    return [(job, JobRun.objects.filter(job=job.name).order_by("-day").first()) for job in jobs()]
