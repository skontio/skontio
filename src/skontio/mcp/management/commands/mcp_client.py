from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from oauth2_provider.models import Application


class Command(BaseCommand):
    help = (
        "Registriert einen MCP-Client vorab (öffentlicher Client mit PKCE), z. B. für Claude: "
        "skontio mcp_client --name Claude --redirect-uri https://claude.ai/api/mcp/auth_callback"
    )

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--redirect-uri", action="append", required=True, dest="redirect_uris")
        parser.add_argument("--owner", default=None, help="Benutzername (Standard: erster Admin)")

    def handle(self, *args, name, redirect_uris, owner, **options):
        for uri in redirect_uris:
            if not (uri.startswith("https://") or uri.startswith(("http://127.0.0.1", "http://localhost"))):
                raise CommandError(f"Redirect-URI muss https sein (oder Loopback): {uri}")
        users = get_user_model().objects
        user = users.filter(username=owner).first() if owner else users.filter(is_staff=True).order_by("pk").first()
        if user is None:
            raise CommandError("Kein Administrator gefunden – zuerst anmelden oder --owner angeben")
        application = Application.objects.create(
            name=name,
            user=user,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=" ".join(redirect_uris),
            skip_authorization=False,
        )
        self.stdout.write(self.style.SUCCESS(f"Client angelegt. Client-ID: {application.client_id}"))
        self.stdout.write("Kein Client-Secret nötig (PKCE). MCP-URL: <öffentliche URL>/mcp")
