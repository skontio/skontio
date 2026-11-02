from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from skontio.web.support import current_company


class Command(BaseCommand):
    help = "Übernimmt den CSV-Export (ZIP) aus Papierkram. Ohne --ausfuehren wird nur geprüft, nichts gespeichert."

    def add_arguments(self, parser):
        parser.add_argument("zip", type=Path)
        parser.add_argument("--ausfuehren", action="store_true", help="wirklich übernehmen (sonst nur prüfen)")
        parser.add_argument("--nummern-nicht-fortsetzen", action="store_true")

    def handle(self, *args, zip, ausfuehren, nummern_nicht_fortsetzen, **options):
        from skontio.imports.papierkram import PapierkramError, import_papierkram

        company = current_company()
        if company is None:
            raise CommandError("Erst die Firma einrichten (Einrichtungsassistent).")
        try:
            report = import_papierkram(company, zip.read_bytes(), dry_run=not ausfuehren,
                                       continue_numbering=not nummern_nicht_fortsetzen)  # fmt: skip
        except (OSError, PapierkramError) as exc:
            raise CommandError(str(exc)) from exc
        for label, count in report.created.items():
            self.stdout.write(f"neu        {label}: {count}")
        for label, count in report.skipped.items():
            self.stdout.write(f"vorhanden  {label}: {count}")
        for line in report.numbering:
            self.stdout.write(f"Nummern    {line}")
        for label, count in report.not_imported.items():
            self.stdout.write(f"nicht übernommen  {label}: {count}")
        for warning in report.warnings:
            self.stdout.write(self.style.WARNING(f"prüfen     {warning}"))
        if report.errors:
            raise CommandError("Nichts übernommen:\n" + "\n".join(report.errors))
        done = "Übernommen." if ausfuehren else "Prüfung ohne Fehler – mit --ausfuehren übernehmen."
        self.stdout.write(self.style.SUCCESS(done))
