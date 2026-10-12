from django.core.management.base import BaseCommand, CommandError

from skontio.audit.models import AuditEvent
from skontio.audit.services import verify_chain


class Command(BaseCommand):
    help = "Prüft die Hash-Kette des Änderungsprotokolls auf Veränderungen und Lücken."

    def handle(self, *args, **options):
        problems = verify_chain()
        if problems:
            raise CommandError("Änderungsprotokoll fehlerhaft:\n" + "\n".join(problems[:50]))
        self.stdout.write(self.style.SUCCESS(f"Änderungsprotokoll in Ordnung ({AuditEvent.objects.count()} Einträge)."))
