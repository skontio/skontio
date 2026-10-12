from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from skontio import __version__
from skontio.ops.backup import BackupError, create_backup


class Command(BaseCommand):
    help = "Spielt Migrationen ein und sichert vorher automatisch, falls eine bestehende Datenbank geändert wird."

    def handle(self, *args, **options):
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        has_data = bool(executor.loader.applied_migrations)
        if pending and has_data:
            try:
                path = create_backup(settings.BACKUP_DIR, reason=f"vor-update-{__version__}")
            except BackupError as exc:
                raise CommandError(f"Backup vor dem Update fehlgeschlagen, Update abgebrochen: {exc}") from exc
            self.stdout.write(f"Backup vor dem Update: {path}")
        call_command("migrate", interactive=False, verbosity=1)
