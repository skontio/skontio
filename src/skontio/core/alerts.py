"""Tell the admins by mail when something unattended goes wrong (backup, Kontoabruf, Beleg-Postfach,
automatischer Rechnungsversand). Never raises: a missing mail server must not break the job that reports."""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.utils import timezone

log = logging.getLogger("skontio.alerts")


def admin_recipients() -> list[str]:
    users = get_user_model().objects.filter(is_staff=True, is_active=True).exclude(email="")
    return sorted(set(users.values_list("email", flat=True)))


def notify_admins(subject: str, lines: list[str]) -> bool:
    """Mail ``lines`` to all admins; returns whether a mail went out."""
    recipients = admin_recipients()
    if not recipients:
        log.warning("Keine Admin-E-Mail-Adresse hinterlegt, Meldung nicht verschickt: %s", subject)
        return False
    body = [
        *lines,
        "",
        f"Zeitpunkt: {timezone.localtime():%d.%m.%Y %H:%M} Uhr",
        f"Skontio: {settings.PUBLIC_URL}",
        "Weitere Einzelheiten: Änderungsprotokoll in Skontio und `skontio doctor worker`.",
    ]
    try:
        EmailMessage(
            f"Skontio: {subject}", "\n".join(body), from_email=settings.DEFAULT_FROM_EMAIL, to=recipients
        ).send()
    except Exception:  # noqa: BLE001 – the problem is logged and recorded; mail is only the extra signal
        log.exception("Meldung an die Admins konnte nicht verschickt werden: %s", subject)
        return False
    return True
