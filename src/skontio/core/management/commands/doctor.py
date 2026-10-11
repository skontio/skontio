import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from skontio.core.doctor import check_oidc


class Command(BaseCommand):
    help = "Prüft die Betriebsumgebung, z. B. `skontio doctor oidc`."
    requires_system_checks = []  # must work even when the rest of the setup is broken

    def add_arguments(self, parser):
        parser.add_argument("target", choices=["oidc"])
        parser.add_argument("--issuer", default=None, help="Standard: SKONTIO_OIDC_ISSUER")

    def handle(self, *args, target, issuer, **options):
        issuer = issuer or settings.OIDC_ISSUER
        if not issuer:
            raise CommandError("SKONTIO_OIDC_ISSUER ist nicht gesetzt")
        with httpx.Client(follow_redirects=False) as client:
            checks = check_oidc(issuer, http_client=client)
        for check in checks:
            mark = self.style.SUCCESS("OK  ") if check.ok else self.style.ERROR("FEHL")
            self.stdout.write(f"{mark} {check.name:<10} {check.detail}")
            if not check.ok and check.hint:
                self.stdout.write(f"     → {check.hint}")
        if not all(check.ok for check in checks):
            raise CommandError("OIDC-Prüfung fehlgeschlagen")
