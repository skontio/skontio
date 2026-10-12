from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from skontio.accounts import totp, twofactor


class Command(BaseCommand):
    help = "Zweiter Faktor für den Notfall-Login per Kommandozeile: skontio totp setup|disable|status <Benutzer>"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["setup", "disable", "status"])
        parser.add_argument("username")
        parser.add_argument("--code", help="Code aus der App (sonst wird interaktiv gefragt)")

    def handle(self, *args, action, username, code, **options):
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"Benutzer {username!r} gibt es nicht")
        if action == "status":
            state = "aktiv" if twofactor.is_enabled(user) else "nicht eingerichtet"
            self.stdout.write(f"2FA für {username}: {state}")
            return
        if action == "disable":
            twofactor.disable(user)
            self.stdout.write(self.style.WARNING(f"2FA für {username} ausgeschaltet – Notfall-Login gesperrt"))
            return
        try:
            secret = twofactor.pending_secret(user) or twofactor.start_setup(user)
        except twofactor.TwoFactorError as exc:
            raise CommandError(str(exc)) from exc
        uri = totp.provisioning_uri(secret, account=username)
        self.stdout.write(f"Schlüssel für die Authenticator-App: {secret}\n{uri}")
        code = code or input("Code aus der App: ")
        try:
            codes = twofactor.confirm_setup(user, code)
        except twofactor.TwoFactorError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("2FA aktiv. Wiederherstellungscodes (einmalig, sicher aufbewahren):"))
        for recovery in codes:
            self.stdout.write(f"  {recovery}")
