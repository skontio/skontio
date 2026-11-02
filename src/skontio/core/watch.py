"""Watch over the company data a fraudster would change: bank account, payment link, sender address.

Classic invoice fraud: someone with a hijacked session swaps the IBAN, and every new invoice asks
customers to pay into the attacker's account. Skontio cannot tell a legitimate change from a fraudulent
one – so every change is recorded and all admins are told at once, by mail, outside the app.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.utils import timezone

from skontio.audit.services import record

log = logging.getLogger("skontio.watch")

WATCHED = {
    "iban": "IBAN",
    "bic": "BIC",
    "account_holder": "Kontoinhaber",
    "payment_link": "Zahlungslink",
    "email": "Absender-/Antwortadresse",
}


def _show(field: str, value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "(leer)"
    if field == "iban":
        return f"… {value[-4:]}"  # enough to recognise, not enough to reuse
    return value[:120]


def snapshot(company) -> dict:
    return {field: getattr(company, field) or "" for field in WATCHED}


def report_changes(company, before: dict, *, user) -> list[str]:
    """Compare with ``before``; record and announce every change. Returns the changed fields."""
    changed = [field for field in WATCHED if (before.get(field) or "") != (getattr(company, field) or "")]
    if not changed:
        return []
    record(
        "company.sensitive_change", actor=user, company=company, entity="company", entity_id=company.pk,
        data={field: {"from": _show(field, before.get(field, "")), "to": _show(field, getattr(company, field))}
              for field in changed},
    )  # fmt: skip
    _notify(company, changed, before, user)
    return changed


def _notify(company, changed: list[str], before: dict, user) -> None:
    admins = get_user_model().objects.filter(is_staff=True, is_active=True).exclude(email="")
    recipients = sorted({*admins.values_list("email", flat=True), *([company.email] if company.email else [])})
    if not recipients:
        return
    who = getattr(user, "get_full_name", lambda: "")() or getattr(user, "username", "") or "unbekannt"
    lines = [
        f"In Skontio wurden am {timezone.localtime():%d.%m.%Y um %H:%M} Uhr Firmendaten geändert (von: {who}):",
        "",
        *[f"- {WATCHED[f]}: {_show(f, before.get(f, ''))} → {_show(f, getattr(company, f))}" for f in changed],
        "",
        "Diese Angaben stehen auf allen neuen Rechnungen. Warst du das nicht, melde dich sofort in Skontio an,",
        "prüfe die Firmendaten und das Änderungsprotokoll und sperre den betroffenen Zugang.",
    ]
    try:
        EmailMessage(
            f"Skontio: Bankverbindung oder Zahlungsangaben von {company.name} geändert",
            "\n".join(lines), from_email=settings.DEFAULT_FROM_EMAIL, to=recipients,
        ).send()  # fmt: skip
    except Exception:  # noqa: BLE001 – the change is saved and recorded; a missing mail server must not undo it
        log.exception("Warnung über geänderte Firmendaten konnte nicht verschickt werden")
