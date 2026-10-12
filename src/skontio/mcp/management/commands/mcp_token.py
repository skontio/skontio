from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from skontio.mcp.models import PersonalAccessToken
from skontio.mcp.tokens import MAX_DAYS, TokenError, active_tokens, create_token, revoke_token


class Command(BaseCommand):
    help = (
        "Persönliche Zugangstokens für MCP (Claude Desktop über skontio-mcp, Claude Code): "
        "skontio mcp_token create <Benutzer> --name 'Claude Desktop' --scope drafts:read --scope drafts:write"
    )

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        create = sub.add_parser("create")
        create.add_argument("username")
        create.add_argument("--name", required=True)
        create.add_argument("--days", type=int, default=30, help=f"1 bis {MAX_DAYS}")
        create.add_argument("--scope", action="append", dest="scopes", default=[], help=", ".join(settings.MCP_SCOPES))
        sub.add_parser("list")
        revoke = sub.add_parser("revoke")
        revoke.add_argument("prefix", help="Anfang des Tokens, z. B. skt_Ab12Cd34")

    def handle(self, *args, action, **options):
        getattr(self, f"_{action}")(**options)

    def _create(self, *, username, name, days, scopes, **_):
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"Benutzer {username} nicht gefunden")
        try:
            token, raw = create_token(user, name=name, scopes=scopes, days=days)
        except TokenError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Token angelegt, gültig bis {token.expires_at:%d.%m.%Y}:"))
        self.stdout.write(raw)
        self.stdout.write("Wird nicht noch einmal angezeigt.")

    def _list(self, **_):
        for token in active_tokens().select_related("user"):
            self.stdout.write(
                f"{token.prefix}…  {token.name}  {token.user}  bis {token.expires_at:%d.%m.%Y}  {token.scopes}"
            )

    def _revoke(self, *, prefix, **_):
        tokens = list(PersonalAccessToken.objects.filter(prefix=prefix, revoked_at__isnull=True))
        if len(tokens) != 1:
            raise CommandError(f"Kein eindeutiger aktiver Token mit Anfang {prefix}")
        revoke_token(tokens[0], actor=None)
        self.stdout.write(self.style.SUCCESS(f"Token {tokens[0].name} widerrufen."))
