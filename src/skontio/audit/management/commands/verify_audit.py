from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from skontio.audit.models import AuditEvent
from skontio.audit.services import verify_anchor, verify_chain


class Command(BaseCommand):
    help = (
        "Prüft die Hash-Kette des Änderungsprotokolls auf Veränderungen und Lücken; mit --backup zusätzlich gegen "
        "das in Sicherungen festgehaltene Kettenende (am besten Sicherungen außer Haus)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--backup", action="append", default=[], help="Backup-Datei (mehrfach möglich)")

    def handle(self, *args, backup, **options):
        from skontio.ops.backup import BackupError, verify_backup

        problems = verify_chain()
        for path in backup:
            try:
                manifest = verify_backup(Path(path))
            except (BackupError, OSError) as exc:
                raise CommandError(f"{path}: {exc}") from exc
            problems += [f"{Path(path).name}: {p}" for p in verify_anchor(manifest.get("audit_head") or {})]
        if problems:
            raise CommandError("Änderungsprotokoll fehlerhaft:\n" + "\n".join(problems[:50]))
        self.stdout.write(self.style.SUCCESS(f"Änderungsprotokoll in Ordnung ({AuditEvent.objects.count()} Einträge)."))
