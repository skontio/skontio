"""Consistent backups of database + archive, verified restore (plan §9).

Format: ``skontio-YYYYMMDD-HHMMSS.tar`` containing
* ``manifest.json``  – version, timestamp, reason, SHA-256 of every member
* ``database.sqlite3`` (SQLite online-backup API) or ``database.pgdump`` (pg_dump custom format)
* ``archive/ab/cd/<sha256>`` – every archived file

The database is dumped first, the archive copied afterwards: the archive is append-only, so every
file the dump references is guaranteed to be in the backup. Files are streamed, never held in memory.

Restore verifies every checksum before writing anything, rejects unexpected paths, writes the
(additive, content-addressed) archive first and replaces the database last and atomically:
SQLite via rename, Postgres in a single transaction (``psql --single-transaction``) – if anything
fails, the previous database stays untouched.

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
ENGINES = {"django.db.backends.sqlite3": "sqlite", "django.db.backends.postgresql": "postgresql"}
READ_ONLY = 0o440
CHUNK = 1024 * 1024
PG_TIMEOUT = 3600


class BackupError(RuntimeError):
    pass


def _hash_stream(handle) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    with open(path, "rb") as handle:
        return _hash_stream(handle)


# --- Postgres tools -------------------------------------------------------------------------------


def pg_tool(name: str) -> str:
    """Locate pg_dump/pg_restore/psql; SKONTIO_PG_BIN_DIR pins the version shipped in the image."""
    path = shutil.which(name, path=settings.PG_BIN_DIR or None)
    if not path:
        raise BackupError(f"{name} ist nicht installiert – das Skontio-Image enthält es, lokal postgresql-client")
    return path


def pg_tool_major(name: str) -> int:
    output = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [pg_tool(name), "--version"], check=True, capture_output=True, text=True, timeout=30
    ).stdout
    match = re.search(r"\)\s*(\d+)", output)
    if not match:
        raise BackupError(f"Version von {name} nicht erkennbar: {output.strip()!r}")
    return int(match.group(1))


def pg_env(database: dict) -> dict:
    """Connection parameters as libpq environment variables, so the password never appears in argv."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    mapping = {"PGHOST": "HOST", "PGPORT": "PORT", "PGUSER": "USER", "PGPASSWORD": "PASSWORD", "PGDATABASE": "NAME"}
    for variable, key in mapping.items():
        if database.get(key):
            env[variable] = str(database[key])
    if sslmode := database.get("OPTIONS", {}).get("sslmode"):
        env["PGSSLMODE"] = sslmode
    return env


def _run_pg(argv: list[str], database: dict, what: str) -> str:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, check=True, env=pg_env(database), capture_output=True, text=True, timeout=PG_TIMEOUT
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise BackupError(f"{what} fehlgeschlagen: {exc.stderr.strip()[-2000:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"{what} hat zu lange gedauert") from exc


def check_pg_dump_version() -> tuple[int, int]:
    """pg_dump refuses newer servers; report that clearly instead of failing at 02:30."""
    connection.ensure_connection()
    server = connection.pg_version // 10000
    client = pg_tool_major("pg_dump")
    if client < server:
        raise BackupError(
            f"pg_dump {client} ist älter als der Postgres-Server {server} – Skontio-Image aktualisieren "
            f"oder SKONTIO_PG_BIN_DIR auf einen pg_dump ≥ {server} zeigen lassen"
        )
    return client, server


# --- create ---------------------------------------------------------------------------------------


def _dump_database(workdir: Path) -> tuple[str, Path, dict]:
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
        return vendor, target, {"tool": f"sqlite {sqlite3.sqlite_version}"}
    if vendor == "postgresql":
        client, server = check_pg_dump_version()
        target = workdir / DB_MEMBERS["postgresql"]
        # one consistent MVCC snapshot; no owner/privileges so the dump restores into any role
        argv = [pg_tool("pg_dump"), "--format=custom", "--no-owner", "--no-privileges", f"--file={target}"]
        _run_pg(argv, connection.settings_dict, "pg_dump")
        return vendor, target, {"tool": f"pg_dump {client}", "server": server}
    raise BackupError(f"Backups für {vendor} werden nicht unterstützt")


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = int(timezone.now().timestamp())
    info.mode = READ_ONLY
    return info


def _add_file(tar: tarfile.TarFile, name: str, path: Path) -> None:
    with open(path, "rb") as handle:
        tar.addfile(_tarinfo(name, os.fstat(handle.fileno()).st_size), handle)


def create_backup(target_dir: Path, *, reason: str = "manual") -> Path:
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"skontio-{timezone.now():%Y%m%d-%H%M%S}.tar"
    archive_root = Path(settings.ARCHIVE_DIR)
    with tempfile.TemporaryDirectory(dir=target_dir, prefix=".work-") as work:
        engine, db_file, db_info = _dump_database(Path(work))
        files: dict[str, Path] = {}
        if archive_root.exists():
            for path in sorted(archive_root.rglob("*")):
                if path.is_file() and not path.name.startswith(".tmp-"):
                    member = f"archive/{path.relative_to(archive_root).as_posix()}"
                    if _file_sha256(path) != path.name:
                        raise BackupError(f"{member} ist beschädigt – Backup abgebrochen (skontio verify_archive)")
                    files[member] = path
        manifest = {
            "format": 1,
            "version": __version__,
            "created_at": timezone.now().isoformat(),
            "reason": reason,
            "database": {"engine": engine, "file": DB_MEMBERS[engine], "sha256": _file_sha256(db_file), **db_info},
            "files": {member: path.name for member, path in files.items()},  # name == verified SHA-256
        }
        partial = target_dir / f".{name}.part"
        with tarfile.open(partial, "w") as tar:
            payload = json.dumps(manifest, indent=2).encode()
            tar.addfile(_tarinfo("manifest.json", len(payload)), io.BytesIO(payload))
            _add_file(tar, DB_MEMBERS[engine], db_file)
            for member, path in files.items():
                _add_file(tar, member, path)
        partial.chmod(0o640)
        final = target_dir / name
        os.replace(partial, final)
    return final


# --- restore --------------------------------------------------------------------------------------


def _open_member(tar: tarfile.TarFile, name: str):
    try:
        member = tar.getmember(name)
    except KeyError as exc:
        raise BackupError(f"{name} fehlt im Backup") from exc
    if not member.isfile():
        raise BackupError(f"{name} ist keine reguläre Datei")
    handle = tar.extractfile(member)
    if handle is None:
        raise BackupError(f"{name} ist nicht lesbar")
    return handle


def _extract_verified(tar: tarfile.TarFile, name: str, digest: str, target: Path) -> None:
    """Copy a member to ``target`` and check the checksum of exactly the bytes written."""
    hasher = hashlib.sha256()
    with _open_member(tar, name) as source, open(target, "wb") as dest:
        while chunk := source.read(CHUNK):
            hasher.update(chunk)
            dest.write(chunk)
    if hasher.hexdigest() != digest:
        target.unlink()
        raise BackupError(f"Prüfsumme von {name} stimmt nicht – Backup beschädigt oder verändert")


def verify_backup(path: Path) -> dict:
    """Check structure and every checksum without writing anything; returns the manifest."""
    with tarfile.open(path) as tar:
        with _open_member(tar, "manifest.json") as handle:
            manifest = json.loads(handle.read())
        engine = manifest.get("database", {}).get("engine")
        if engine not in DB_MEMBERS:
            raise BackupError(f"Unbekannte Datenbank im Backup: {engine!r}")
        files = manifest.get("files", {})
        for member, digest in files.items():
            if not ARCHIVE_MEMBER.match(member) or member.rsplit("/", 1)[-1] != digest:
                raise BackupError(f"Unzulässiger Pfad im Backup: {member!r}")
        unexpected = set(tar.getnames()) - {"manifest.json", DB_MEMBERS[engine], *files}
        if unexpected:
            raise BackupError(f"Unerwartete Dateien im Backup: {sorted(unexpected)[:3]}")
        with _open_member(tar, DB_MEMBERS[engine]) as handle:
            if _hash_stream(handle) != manifest["database"]["sha256"]:
                raise BackupError("Prüfsumme der Datenbank stimmt nicht – Backup beschädigt oder verändert")
        for member, digest in files.items():
            with _open_member(tar, member) as handle:
                if _hash_stream(handle) != digest:
                    raise BackupError(f"Prüfsumme von {member} stimmt nicht – Backup beschädigt oder verändert")
    return manifest


def _restore_archive(tar: tarfile.TarFile, files: dict, archive_dir: Path) -> None:
    for member, digest in files.items():
        target = archive_dir / member.removeprefix("archive/")
        if target.exists() and _file_sha256(target) == digest:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".tmp-restore-{digest}")
        _extract_verified(tar, member, digest, partial)
        partial.chmod(READ_ONLY)
        if target.exists():
            target.chmod(0o600)
        os.replace(partial, target)


def _restore_sqlite(tar: tarfile.TarFile, manifest: dict, database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    partial = database_path.with_name(f".{database_path.name}.restore")
    _extract_verified(tar, DB_MEMBERS["sqlite"], manifest["database"]["sha256"], partial)
    for suffix in ("-wal", "-shm"):  # the old database's WAL must not be replayed onto the restored one
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)
    os.replace(partial, database_path)


def postgres_has_tables(database: dict) -> bool:
    query = "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'"
    output = _run_pg([pg_tool("psql"), "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", query], database, "Postgres")
    return int(output.strip() or 0) > 0


def _restore_postgres(tar: tarfile.TarFile, manifest: dict, database: dict, workdir: Path) -> None:
    dump, script = workdir / DB_MEMBERS["postgresql"], workdir / "restore.sql"
    _extract_verified(tar, DB_MEMBERS["postgresql"], manifest["database"]["sha256"], dump)
    argv = [pg_tool("pg_restore"), "--no-owner", "--no-privileges", f"--file={script}", str(dump)]
    _run_pg(argv, database, "pg_restore")
    # Replace the whole schema in ONE transaction: on any error the old database stays as it was.
    argv = [pg_tool("psql"), "-X", "-q", "--single-transaction", "-v", "ON_ERROR_STOP=1"]
    argv += ["-c", "DROP SCHEMA IF EXISTS public CASCADE", "-c", "CREATE SCHEMA public", "-f", str(script)]
    _run_pg(argv, database, "Einspielen in Postgres")


def restore_backup(path: Path, *, database: dict, archive_dir: Path, force: bool = False) -> dict:
    """Verify everything first, then write. The application must be stopped while restoring.

    ``database`` is a Django DATABASES entry; its engine must match the backup.
    """
    manifest = verify_backup(path)
    engine = manifest["database"]["engine"]
    target_engine = ENGINES.get(database.get("ENGINE", ""))
    if engine != target_engine:
        raise BackupError(f"Backup enthält eine {engine}-Datenbank, eingestellt ist {target_engine}")
    if engine == "postgresql":
        occupied, where = postgres_has_tables(database), f"Datenbank {database['NAME']}"
    else:
        occupied, where = Path(database["NAME"]).exists(), str(database["NAME"])
    if occupied and not force:
        raise BackupError(f"{where} existiert bereits bzw. ist nicht leer – mit --force überschreiben")

    # work files next to the backup: /tmp is a small tmpfs in the container
    with tarfile.open(path) as tar, tempfile.TemporaryDirectory(dir=Path(path).parent, prefix=".restore-") as work:
        _restore_archive(tar, manifest["files"], Path(archive_dir))
        if engine == "sqlite":
            _restore_sqlite(tar, manifest, Path(database["NAME"]))
        else:
            _restore_postgres(tar, manifest, database, Path(work))
    return manifest


# --- maintenance ----------------------------------------------------------------------------------


def verify_archive(archive_dir: Path) -> list[str]:
    """Return a list of problems; every archived file is named after its SHA-256."""
    problems = []
    root = Path(archive_dir)
    for path in sorted(root.rglob("*")) if root.exists() else []:
        if not path.is_file() or path.name.startswith(".tmp-"):
            continue
        if _file_sha256(path) != path.name:
            problems.append(f"{path.relative_to(root).as_posix()}: Inhalt passt nicht zur Prüfsumme")
    return problems


def prune_backups(directory: Path, *, keep: int) -> list[Path]:
    backups = sorted(p for p in Path(directory).iterdir() if p.is_file() and BACKUP_NAME.match(p.name))
    removed = backups[:-keep] if keep > 0 else backups
    for path in removed:
        path.unlink()
    return removed
