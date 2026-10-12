from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from skontio.ops.backup import BackupError, restore_backup


class Command(BaseCommand):
    help = "Spielt ein Backup ein (SQLite oder Postgres). Vorher die App stoppen."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("backup", type=Path)
        parser.add_argument("--force", action="store_true", help="vorhandene Datenbank überschreiben")

    def handle(self, *args, backup, force, **options):
        connection.close()  # Postgres: no open session may hold locks on the schema being replaced
        try:
            manifest = restore_backup(
                backup, database=settings.DATABASES["default"], archive_dir=settings.ARCHIVE_DIR, force=force
            )
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Backup vom {manifest['created_at']} (Version {manifest['version']}, "
                f"{len(manifest['files'])} Archivdateien) eingespielt. Beim nächsten Start laufen ggf. Migrationen."
            )
        )
