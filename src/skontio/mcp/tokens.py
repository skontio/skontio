"""Personal access tokens (``skt_…``) for MCP clients that cannot do OAuth (plan §7.2).

* Only MCP scopes, at most 90 days, only for administrators (same rule as the OAuth consent).
* The plain token is shown once; the database keeps its SHA-256 (256 random bits, no salt needed).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from skontio.audit.services import record
from skontio.mcp.models import PersonalAccessToken

TOKEN_PREFIX = "skt_"  # noqa: S105 – public marker, not a secret
MAX_DAYS = 90
LAST_USED_RESOLUTION = dt.timedelta(minutes=5)  # avoid one UPDATE per MCP call


class TokenError(ValueError):
    pass


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_token(user, *, name: str, scopes, days: int = 30) -> tuple[PersonalAccessToken, str]:
    """Return the stored token and the plain value (shown once, never stored)."""
    name, scopes = name.strip(), sorted(set(scopes))
    if not user.is_active or not user.is_staff:
        raise TokenError("Nur Administratoren dürfen Zugangstokens für KI-Clients anlegen.")
    if not name:
        raise TokenError("Bitte einen Namen angeben (z. B. „Claude Desktop“).")
    if not scopes:
        raise TokenError("Mindestens eine Berechtigung auswählen.")
    if unknown := [s for s in scopes if s not in settings.MCP_SCOPES]:
        raise TokenError(f"Unbekannte Berechtigung: {', '.join(unknown)}")
    if not 1 <= days <= MAX_DAYS:
        raise TokenError(f"Gültigkeit 1 bis {MAX_DAYS} Tage.")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with transaction.atomic():
        token = PersonalAccessToken.objects.create(
            user=user,
            name=name[:100],
            prefix=raw[:12],
            token_hash=_hash(raw),
            scopes=" ".join(scopes),
            expires_at=timezone.now() + dt.timedelta(days=days),
        )
        record(
            "mcp.pat.create",
            actor=user,
            entity="personal_access_token",
            entity_id=token.pk,
            data={"name": token.name, "scopes": scopes, "expires_at": token.expires_at.isoformat()},
        )
    return token, raw


def authenticate_pat(raw: str) -> PersonalAccessToken | None:
    if not raw.startswith(TOKEN_PREFIX):
        return None
    token = PersonalAccessToken.objects.select_related("user").filter(token_hash=_hash(raw)).first()
    now = timezone.now()
    if token is None or token.revoked_at or token.expires_at <= now or not token.user.is_active:
        return None
    if token.last_used_at is None or now - token.last_used_at > LAST_USED_RESOLUTION:
        PersonalAccessToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return token


def revoke_token(token: PersonalAccessToken, *, actor) -> None:
    if token.revoked_at:
        return
    with transaction.atomic():
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        record("mcp.pat.revoke", actor=actor, entity="personal_access_token", entity_id=token.pk)


def active_tokens():
    return PersonalAccessToken.objects.filter(revoked_at__isnull=True, expires_at__gt=timezone.now())
