"""Content-addressed archive on the filesystem (``SKONTIO_ARCHIVE_DIR``).

Files are written once, made read-only and verified against their SHA-256 on every read,
so silent corruption or manipulation is detected. ZFS snapshots and off-site replication
on the NAS add the second line of defence (plan §9).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from skontio.archive.models import Blob

READ_ONLY = 0o440


class ArchiveCorrupted(RuntimeError):
    """The stored file is missing or no longer matches its recorded SHA-256."""


def _root() -> Path:
    return Path(settings.ARCHIVE_DIR)


def _key(digest: str) -> str:
    return f"{digest[:2]}/{digest[2:4]}/{digest}"


def store(data: bytes, *, mime: str, retention_until: dt.date | None = None) -> Blob:
    digest = hashlib.sha256(data).hexdigest()
    existing = Blob.objects.filter(sha256=digest).first()
    if existing:
        if retention_until and (existing.retention_until is None or existing.retention_until < retention_until):
            existing.retention_until = retention_until
            existing.save(update_fields=["retention_until"])
        return existing

    key = _key(digest)
    path = _root() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ArchiveCorrupted(f"{key} existiert bereits mit anderem Inhalt")
    else:
        # write to a temp file in the same directory, fsync, then atomically rename
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, READ_ONLY)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    try:
        with transaction.atomic():
            return Blob.objects.create(
                sha256=digest, size=len(data), mime=mime, storage_key=key, retention_until=retention_until
            )
    except IntegrityError:
        return Blob.objects.get(sha256=digest)


def load(blob: Blob) -> bytes:
    path = _root() / blob.storage_key
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ArchiveCorrupted(f"Archivdatei {blob.storage_key} fehlt") from exc
    if hashlib.sha256(data).hexdigest() != blob.sha256:
        raise ArchiveCorrupted(f"Archivdatei {blob.storage_key} wurde verändert")
    return data


def retention_end(document_date: dt.date, years: int) -> dt.date:
    """Retention starts at the end of the calendar year (§ 147 Abs. 4 AO)."""
    return dt.date(document_date.year + years, 12, 31)


def release(blob: Blob) -> bool:
    """Delete a blob that nothing references any more and that has no running retention or legal hold.

    Returns whether it was deleted. Everything under retention (§ 147 AO) stays until redaction.
    """
    if blob.legal_hold or (blob.retention_until and blob.retention_until >= timezone.localdate()):
        return False
    path = _root() / blob.storage_key
    try:
        with transaction.atomic():
            blob.delete()  # PROTECT foreign keys (renditions, inbox, attachments) refuse while referenced
    except ProtectedError:
        return False
    transaction.on_commit(lambda: path.unlink(missing_ok=True))
    return True
