"""Backup und Wiederherstellung: Datenbank + revisionssicheres Archiv, manipulationssicher geprüft."""

from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
import tarfile

import pytest
from django.db import connection

from skontio.archive.services import store
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import create_draft
from skontio.ops.backup import BackupError, create_backup, prune_backups, restore_backup, verify_archive

pytestmark = pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite backup path (Postgres uses pg_dump)")


@pytest.fixture
def data(db):
    # drafts only: transactional tests flush SQLite with DELETE, which the lock triggers refuse for finalized rows
    company = Company.objects.create(name="Backup-Firma", tax_number="1/2/3")
    customer = create_customer(company, name="Kunde", street="W", postcode="2", city="K")
    create_draft(company, Document.Type.INVOICE, customer, issue_date=dt.date(2026, 10, 1))
    return store(b"%PDF-1.7 Rechnung", mime="application/pdf")


@pytest.fixture
def backup_dir(tmp_path):
    path = tmp_path / "backups"
    path.mkdir()
    return path


@pytest.mark.django_db(transaction=True)
class TestBackup:
    def test_backup_contains_database_archive_and_manifest(self, data, backup_dir):
        path = create_backup(backup_dir, reason="test")
        with tarfile.open(path) as tar:
            names = set(tar.getnames())
            manifest = json.load(tar.extractfile("manifest.json"))
        assert "database.sqlite3" in names
        assert manifest["database"]["engine"] == "sqlite"
        assert f"archive/{data.storage_key}" in names
        assert set(manifest["files"]) == {name for name in names if name.startswith("archive/")}
        assert manifest["reason"] == "test"
        dt.datetime.fromisoformat(manifest["created_at"])

    def test_restore_reproduces_database_and_archive(self, data, backup_dir, tmp_path):
        path = create_backup(backup_dir, reason="test")
        target_db, target_archive = tmp_path / "restore" / "db.sqlite3", tmp_path / "restore" / "archive"
        restore_backup(path, database_path=target_db, archive_dir=target_archive)
        with sqlite3.connect(target_db) as con:
            name = con.execute("SELECT name FROM core_company").fetchone()[0]
            triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert name == "Backup-Firma"
        assert "skontio_document_no_update" in triggers  # immutability survives the restore
        assert (target_archive / data.storage_key).read_bytes() == b"%PDF-1.7 Rechnung"
        assert verify_archive(target_archive) == []

    def test_restore_refuses_tampered_backup(self, data, backup_dir, tmp_path):
        path = create_backup(backup_dir, reason="test")
        tampered = tmp_path / "tampered.tar"
        with tarfile.open(path) as src, tarfile.open(tampered, "w") as dst:
            for member in src.getmembers():
                content = src.extractfile(member).read()
                if member.name.startswith("archive/"):
                    content += b"manipuliert"
                    member.size = len(content)
                dst.addfile(member, io.BytesIO(content))
        with pytest.raises(BackupError, match="Prüfsumme"):
            restore_backup(tampered, database_path=tmp_path / "x" / "db.sqlite3", archive_dir=tmp_path / "x" / "a")

    def test_restore_refuses_existing_target_without_force(self, data, backup_dir, tmp_path):
        path = create_backup(backup_dir, reason="test")
        target_db = tmp_path / "db.sqlite3"
        target_db.write_bytes(b"existing")
        with pytest.raises(BackupError, match="existiert"):
            restore_backup(path, database_path=target_db, archive_dir=tmp_path / "archive")


def test_restore_rejects_path_traversal(tmp_path):
    evil = tmp_path / "evil.tar"
    manifest = {"version": "x", "files": {"../../etc/x": "00"}, "database": {"engine": "sqlite", "sha256": "00"}}
    with tarfile.open(evil, "w") as tar:
        payload = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(BackupError):
        restore_backup(evil, database_path=tmp_path / "r" / "db.sqlite3", archive_dir=tmp_path / "r" / "archive")


@pytest.mark.django_db
def test_verify_archive_detects_corruption(settings):
    blob = store(b"original", mime="text/plain")
    path = settings.ARCHIVE_DIR / blob.storage_key
    path.chmod(0o600)
    path.write_bytes(b"kaputt")
    problems = verify_archive(settings.ARCHIVE_DIR)
    assert problems and blob.storage_key in problems[0]


def test_prune_keeps_newest(tmp_path):
    for day in range(1, 6):
        (tmp_path / f"skontio-2026100{day}-020000.tar").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_text("bleibt")
    removed = prune_backups(tmp_path, keep=2)
    assert len(removed) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "skontio-20261004-020000.tar",
        "skontio-20261005-020000.tar",
        "unrelated.txt",
    ]
