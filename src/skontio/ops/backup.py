"""Consistent backups of database + archive, verified restore (plan §9).

Format: ``skontio-YYYYMMDD-HHMMSS.tar`` containing
* ``manifest.json``  – version, timestamp, reason, SHA-256 of every member
* ``database.sqlite3`` (SQLite online-backup API) or ``database.pgdump`` (pg_dump -Fc)
* ``archive/ab/cd/<sha256>`` – every archived file

Restore verifies every checksum before writing anything and rejects unexpected paths.
ZFS snapshots and off-site replication on the NAS complement these backups.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from skontio import __version__

BACKUP_NAME = re.compile(r"^skontio-\d{8}-\d{6}\.tar$")
ARCHIVE_MEMBER = re.compile(r"^archive/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$")
DB_MEMBERS = {"sqlite": "database.sqlite3", "postgresql": "database.pgdump"}
READ_ONLY = 0o440


class BackupError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump_database(workdir: Path) -> tuple[str, Path]:
    vendor = connection.vendor
    if vendor == "sqlite":
        target = workdir / DB_MEMBERS["sqlite"]
        source = sqlite3.connect(connection.settings_dict["NAME"])
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)  # consistent online copy, also while the app is running
        finally:
            dest.close()
            source.close()
        return vendor, target
    if vendor == "postgresql":
        pg_dump = shutil.which("pg_dump")
        if not pg_dump:
            raise BackupError("pg_dump ist nicht installiert – für Postgres-Backups das Postgres-Client-Paket ergänzen")
        params = connection.settings_dict
        target = workdir / DB_MEMBERS["postgresql"]
        env = {**os.environ, "PGPASSWORD": params.get("PASSWORD", "")}
        command = [
            pg_dump,
            "-Fc",
            "-h",
            params["HOST"],
            "-p",
            str(params["PORT"] or 5432),
            "-U",
            params["USER"],
            "-f",
            str(target),
            params["NAME"],
        ]
        subprocess.run(command, check=True, env=env, timeout=3600)  # noqa: S603 - fixed argv, no shell
        return vendor, target
    raise BackupError(f"Backups für {vendor} werden nicht unterstützt")


def _add(tar: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mtime = int(timezone.now().timestamp())
    info.mode = 0o440
    tar.addfile(info, io.BytesIO(content))


def create_backup(target_dir: Path, *, reason: str = "manual") -> Path:
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"skontio-{timezone.now():%Y%m%d-%H%M%S}.tar"
    archive_root = Path(settings.ARCHIVE_DIR)
    with tempfile.TemporaryDirectory(dir=target_dir, prefix=".work-") as work:
        engine, db_file = _dump_database(Path(work))
        db_bytes = db_file.read_bytes()
        members: list[tuple[str, bytes]] = []
        if archive_root.exists():
            for path in sorted(archive_root.rglob("*")):
                if path.is_file() and not path.name.startswith(".tmp-"):
                    members.append((f"archive/{path.relative_to(archive_root).as_posix()}", path.read_bytes()))
        manifest = {
            "format": 1,
            "version": __version__,
            "created_at": timezone.now().isoformat(),
            "reason": reason,
            "database": {"engine": engine, "file": DB_MEMBERS[engine], "sha256": _sha256(db_bytes)},
            "files": {member: _sha256(content) for member, content in members},
        }
        partial = target_dir / f".{name}.part"
        with tarfile.open(partial, "w") as tar:
            _add(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
            _add(tar, DB_MEMBERS[engine], db_bytes)
            for member, content in members:
                _add(tar, member, content)
        partial.chmod(0o640)
        final = target_dir / name
        os.replace(partial, final)
    return final


def _read_member(tar: tarfile.TarFile, name: str) -> bytes:
    try:
        member = tar.getmember(name)
    except KeyError as exc:
        raise BackupError(f"{name} fehlt im Backup") from exc
    if not member.isfile():
        raise BackupError(f"{name} ist keine reguläre Datei")
    handle = tar.extractfile(member)
    if handle is None:
        raise BackupError(f"{name} ist nicht lesbar")
    return handle.read()


def restore_backup(path: Path, *, database_path: Path, archive_dir: Path, force: bool = False) -> dict:
    """Verify everything first, then write. The application must be stopped while restoring."""
    database_path, archive_dir = Path(database_path), Path(archive_dir)
    with tarfile.open(path) as tar:
        manifest = json.loads(_read_member(tar, "manifest.json"))
        engine = manifest.get("database", {}).get("engine")
        if engine not in DB_MEMBERS:
            raise BackupError(f"Unbekannte Datenbank im Backup: {engine!r}")
        for member in manifest.get("files", {}):
            if not ARCHIVE_MEMBER.match(member):
                raise BackupError(f"Unzulässiger Pfad im Backup: {member!r}")
        unexpected = set(tar.getnames()) - {"manifest.json", DB_MEMBERS[engine], *manifest["files"]}
        if unexpected:
            raise BackupError(f"Unerwartete Dateien im Backup: {sorted(unexpected)[:3]}")

        database = _read_member(tar, DB_MEMBERS[engine])
        if _sha256(database) != manifest["database"]["sha256"]:
            raise BackupError("Prüfsumme der Datenbank stimmt nicht – Backup beschädigt oder verändert")
        contents = {}
        for member, digest in manifest["files"].items():
            content = _read_member(tar, member)
            if _sha256(content) != digest or member.rsplit("/", 1)[-1] != digest:
                raise BackupError(f"Prüfsumme von {member} stimmt nicht – Backup beschädigt oder verändert")
            contents[member] = content

    if engine != "sqlite":
        raise BackupError("Postgres-Backups bitte mit pg_restore einspielen (siehe docs/betrieb.md)")
    if database_path.exists() and not force:
        raise BackupError(f"{database_path} existiert bereits – mit --force überschreiben")

    database_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = database_path.with_name(f".{database_path.name}.restore")
    tmp_db.write_bytes(database)
    os.replace(tmp_db, database_path)
    for member, content in contents.items():
        target = archive_dir / member.removeprefix("archive/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.chmod(0o600)
        target.write_bytes(content)
        target.chmod(READ_ONLY)
    return manifest


def verify_archive(archive_dir: Path) -> list[str]:
    """Return a list of problems; every archived file is named after its SHA-256."""
    problems = []
    root = Path(archive_dir)
    for path in sorted(root.rglob("*")) if root.exists() else []:
        if not path.is_file() or path.name.startswith(".tmp-"):
            continue
        if _sha256(path.read_bytes()) != path.name:
            problems.append(f"{path.relative_to(root).as_posix()}: Inhalt passt nicht zur Prüfsumme")
    return problems


def prune_backups(directory: Path, *, keep: int) -> list[Path]:
    backups = sorted(p for p in Path(directory).iterdir() if p.is_file() and BACKUP_NAME.match(p.name))
    removed = backups[:-keep] if keep > 0 else backups
    for path in removed:
        path.unlink()
    return removed
