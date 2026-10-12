from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from skontio.ops.backup import BackupError, create_backup, prune_backups, verify_archive


class Command(BaseCommand):
    help = "Erstellt ein geprüftes Backup von Datenbank und Archiv (für TrueNAS-Cronjob oder vor Updates)."

    def add_arguments(self, parser):
        parser.add_argument("--reason", default="manual")
        parser.add_argument("--keep", type=int, default=settings.BACKUP_KEEP, help="Anzahl der aufbewahrten Backups")

    def handle(self, *args, reason, keep, **options):
        problems = verify_archive(settings.ARCHIVE_DIR)
        if problems:
            raise CommandError("Archiv beschädigt – Backup abgebrochen:\n" + "\n".join(problems[:20]))
        try:
            path = create_backup(settings.BACKUP_DIR, reason=reason)
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        removed = prune_backups(settings.BACKUP_DIR, keep=keep)
        self.stdout.write(self.style.SUCCESS(f"Backup erstellt: {path}"))
        if removed:
            self.stdout.write(f"{len(removed)} ältere Backups entfernt")
