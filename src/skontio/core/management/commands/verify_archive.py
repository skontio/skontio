from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from skontio.archive.models import Blob
from skontio.ops.backup import verify_archive


class Command(BaseCommand):
    help = "Prüft alle Archivdateien gegen ihre SHA-256-Prüfsumme und meldet fehlende Dateien."

    def handle(self, *args, **options):
        problems = verify_archive(settings.ARCHIVE_DIR)
        missing = [b.storage_key for b in Blob.objects.all() if not (settings.ARCHIVE_DIR / b.storage_key).exists()]
        problems += [f"{key}: Datei fehlt" for key in missing]
        if problems:
            raise CommandError("Archiv fehlerhaft:\n" + "\n".join(problems[:50]))
        self.stdout.write(self.style.SUCCESS(f"Archiv in Ordnung ({Blob.objects.count()} Dateien)."))
