"""Postgres-Backups mit pg_dump und atomarer Wiederherstellung (psql --single-transaction)."""

from __future__ import annotations

import datetime as dt
import io
import shutil
import tarfile
import uuid

import psycopg
import pytest
from django.conf import settings as django_settings
from django.db import connection

from skontio.archive.services import store
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import create_draft
from skontio.ops import backup
from skontio.ops.backup import BackupError, create_backup, restore_backup, verify_archive

pytestmark = [
    pytest.mark.skipif(connection.vendor != "postgresql", reason="Postgres only"),
    pytest.mark.skipif(
        not all(shutil.which(t, path=django_settings.PG_BIN_DIR or None) for t in ("pg_dump", "pg_restore", "psql")),
        reason="pg_dump/pg_restore/psql not installed",
    ),
]


@pytest.fixture
def data(transactional_db):
    company = Company.objects.create(name="Postgres-Firma", tax_number="1/2/3")
    customer = create_customer(company, name="Kunde", street="W", postcode="2", city="K")
    create_draft(company, Document.Type.INVOICE, customer, issue_date=dt.date(2026, 10, 1))
    return store(b"%PDF-1.7 Rechnung", mime="application/pdf")


@pytest.fixture
def target(transactional_db):
    """An extra empty database next to the test database, dropped afterwards."""
    name = f"skontio_restore_{uuid.uuid4().hex[:8]}"
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')
    yield {**connection.settings_dict, "NAME": name}
    with connection.cursor() as cursor:
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def query(database: dict, sql: str) -> list[tuple]:
    params = {"host": database["HOST"] or None, "port": database["PORT"] or None, "user": database["USER"]}
    with psycopg.connect(dbname=database["NAME"], password=database["PASSWORD"], **params) as con:
        cursor = con.execute(sql)
        return cursor.fetchall() if cursor.description else []


def test_backup_contains_pg_dump(data, tmp_path):
    path = create_backup(tmp_path, reason="test")
    manifest = backup.verify_backup(path)
    assert manifest["database"]["engine"] == "postgresql"
    assert manifest["database"]["tool"].startswith("pg_dump ")
    with tarfile.open(path) as tar:
        assert tar.extractfile("database.pgdump").read(5) == b"PGDMP"  # custom format


def test_restore_into_empty_database(data, target, tmp_path):
    path = create_backup(tmp_path / "backups", reason="test")
    archive = tmp_path / "restored"
    restore_backup(path, database=target, archive_dir=archive)
    assert query(target, "SELECT name FROM core_company") == [("Postgres-Firma",)]
    assert query(target, "SELECT count(*) FROM documents_document") == [(1,)]
    triggers = {row[0] for row in query(target, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")}
    assert {"skontio_document_guard", "skontio_line_guard"} <= triggers  # immutability survives the restore
    assert (archive / data.storage_key).read_bytes() == b"%PDF-1.7 Rechnung"
    assert verify_archive(archive) == []


def test_non_empty_database_needs_force(data, target, tmp_path):
    path = create_backup(tmp_path / "backups", reason="test")
    restore_backup(path, database=target, archive_dir=tmp_path / "restored")
    with pytest.raises(BackupError, match="nicht leer"):
        restore_backup(path, database=target, archive_dir=tmp_path / "restored")
    query(target, "UPDATE core_company SET name = 'geändert'")
    restore_backup(path, database=target, archive_dir=tmp_path / "restored", force=True)
    assert query(target, "SELECT name FROM core_company") == [("Postgres-Firma",)]


def test_failed_restore_leaves_old_database_untouched(data, target, tmp_path, monkeypatch):
    path = create_backup(tmp_path / "backups", reason="test")
    restore_backup(path, database=target, archive_dir=tmp_path / "restored")
    query(target, "UPDATE core_company SET name = 'Stand vor dem Restore'")

    real_run = backup._run_pg

    def sabotage(argv, database, what):
        output = real_run(argv, database, what)
        if what == "pg_restore":  # break the script after all tables were dropped and recreated
            script = next(arg.removeprefix("--file=") for arg in argv if arg.startswith("--file="))
            with open(script, "a", encoding="utf-8") as handle:
                handle.write("\nSELECT 1/0;\n")
        return output

    monkeypatch.setattr(backup, "_run_pg", sabotage)
    with pytest.raises(BackupError, match="Einspielen"):
        restore_backup(path, database=target, archive_dir=tmp_path / "restored", force=True)
    assert query(target, "SELECT name FROM core_company") == [("Stand vor dem Restore",)]


def test_tampered_dump_is_refused_before_writing(data, target, tmp_path):
    path = create_backup(tmp_path / "backups", reason="test")
    tampered = tmp_path / "tampered.tar"
    with tarfile.open(path) as src, tarfile.open(tampered, "w") as dst:
        for member in src.getmembers():
            content = src.extractfile(member).read()
            if member.name == "database.pgdump":
                content = content[:-1] + bytes([content[-1] ^ 1])
            dst.addfile(member, io.BytesIO(content))
    with pytest.raises(BackupError, match="Prüfsumme der Datenbank"):
        restore_backup(tampered, database=target, archive_dir=tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert query(target, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == [(0,)]


def test_old_pg_dump_is_reported(data, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "pg_tool_major", lambda name: 9)
    with pytest.raises(BackupError, match="älter als der Postgres-Server"):
        create_backup(tmp_path, reason="test")
