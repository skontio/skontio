"""Zahlungslink (PayPal.me o. Ä.): a configurable URL printed on invoices and in the mail.

Skontio only shows the link; the payment itself happens at the provider (no payment service
under the ZAG). Placeholders: {betrag} → amount due as 123.45, {nummer} → invoice number.
"""

from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import quote, urlsplit

from django.core.exceptions import ValidationError

PLACEHOLDERS = {"betrag", "nummer"}
MAX_LENGTH = 300
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_FORBIDDEN = re.compile(r"[\s\"'<>\\`^|{}]")


def validate_template(value: str) -> None:
    if not value:
        return
    if len(value) > MAX_LENGTH:
        raise ValidationError(f"Höchstens {MAX_LENGTH} Zeichen.")
    unknown = sorted({name for name in _PLACEHOLDER.findall(value) if name not in PLACEHOLDERS})
    if unknown:
        raise ValidationError(
            f"Unbekannter Platzhalter: {', '.join(unknown)} – erlaubt sind {{betrag}} und {{nummer}}."
        )
    probe = _PLACEHOLDER.sub("x", value)
    if _FORBIDDEN.search(probe) or any(ord(ch) < 32 for ch in probe):
        raise ValidationError("Der Link enthält unzulässige Zeichen (Leerzeichen, Anführungszeichen, Klammern).")
    try:
        parts = urlsplit(probe)
        host = parts.hostname
    except ValueError as exc:
        raise ValidationError("Das ist kein gültiger Link.") from exc
    if parts.scheme != "https" or not host or "." not in host or parts.username or parts.password:
        raise ValidationError("Bitte einen vollständigen https-Link angeben, z. B. https://paypal.me/name/{betrag}EUR.")


def build(template: str, *, amount: Decimal, number: str) -> str:
    """The concrete link; empty if no template is set or the template is not valid."""
    if not template:
        return ""
    try:
        validate_template(template)
    except ValidationError:
        return ""
    values = {"betrag": f"{amount.quantize(Decimal('0.01'))}", "nummer": quote(number or "", safe="")}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
