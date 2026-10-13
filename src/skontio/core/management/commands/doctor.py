import httpx
from django.conf import settings
from django.core.mail import mailers
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from skontio.core.doctor import check_backup, check_oidc, check_smtp, check_worker
from skontio.ops import scheduler
from skontio.ops.backup import check_pg_dump_version


class Command(BaseCommand):
    help = "Prüft die Betriebsumgebung, z. B. `skontio doctor oidc|smtp|backup|worker`."
    requires_system_checks = []  # must work even when the rest of the setup is broken

    def add_arguments(self, parser):
        parser.add_argument("target", choices=["oidc", "smtp", "backup", "worker"])
        parser.add_argument("--issuer", default=None, help="Standard: SKONTIO_OIDC_ISSUER")

    def handle(self, *args, target, issuer, **options):
        if target == "oidc":
            issuer = issuer or settings.OIDC_ISSUER
            if not issuer:
                raise CommandError("SKONTIO_OIDC_ISSUER ist nicht gesetzt")
            with httpx.Client(follow_redirects=False) as client:
                checks = check_oidc(issuer, http_client=client)
        elif target == "backup":
            checks = check_backup(settings.BACKUP_DIR, vendor=connection.vendor, pg_versions=check_pg_dump_version)
        elif target == "worker":
            checks = check_worker(scheduler.status(), today=timezone.localdate())
        else:
            host = settings.MAILERS.get("default", {}).get("OPTIONS", {}).get("host", "")
            checks = check_smtp(mailers.default, host=host)
        for check in checks:
            mark = self.style.SUCCESS("OK  ") if check.ok else self.style.ERROR("FEHL")
            self.stdout.write(f"{mark} {check.name:<10} {check.detail}")
            if not check.ok and check.hint:
                self.stdout.write(f"     → {check.hint}")
        if not all(check.ok for check in checks):
            raise CommandError(f"{target.upper()}-Prüfung fehlgeschlagen")
