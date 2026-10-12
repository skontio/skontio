"""Second factor for the local break-glass login (plan §10): TOTP plus one-time recovery codes."""

from __future__ import annotations

import hashlib
import secrets

from django.db import transaction
from django.utils import timezone

from skontio.accounts import totp
from skontio.accounts.models import RecoveryCode, TotpDevice
from skontio.audit.services import record
from skontio.core.crypto import seal, unseal

PURPOSE = "totp"
RECOVERY_CODES = 10


class TwoFactorError(ValueError):
    pass


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().replace("-", "").lower().encode()).hexdigest()


def is_enabled(user) -> bool:
    return TotpDevice.objects.filter(user=user, confirmed_at__isnull=False).exists()


def start_setup(user) -> str:
    """New unconfirmed seed (replaces an earlier unconfirmed one). Returns the base32 secret."""
    if is_enabled(user):
        raise TwoFactorError("2FA ist bereits eingerichtet – zuerst ausschalten")
    secret = totp.generate_secret()
    TotpDevice.objects.update_or_create(user=user, defaults={"secret_sealed": seal(secret, purpose=PURPOSE)})
    return secret


def pending_secret(user) -> str | None:
    device = TotpDevice.objects.filter(user=user, confirmed_at__isnull=True).first()
    return unseal(device.secret_sealed, purpose=PURPOSE) if device else None


def confirm_setup(user, code: str) -> list[str]:
    """Activate after the first correct code. Returns new recovery codes (shown once)."""
    with transaction.atomic():
        device = TotpDevice.objects.select_for_update().filter(user=user, confirmed_at__isnull=True).first()
        if device is None:
            raise TwoFactorError("Keine Einrichtung begonnen")
        step = totp.verify(unseal(device.secret_sealed, purpose=PURPOSE), code)
        if step is None:
            raise TwoFactorError("Der Code stimmt nicht – Uhrzeit des Telefons prüfen")
        device.confirmed_at, device.last_used_step = timezone.now(), step
        device.save(update_fields=["confirmed_at", "last_used_step"])
        codes = _new_recovery_codes(user)
        record("auth.2fa_enabled", actor=user, entity="user", entity_id=user.pk)
    return codes


def _new_recovery_codes(user) -> list[str]:
    RecoveryCode.objects.filter(user=user).delete()
    codes = [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(RECOVERY_CODES)]
    RecoveryCode.objects.bulk_create(RecoveryCode(user=user, code_hash=_hash(c)) for c in codes)
    return codes


def verify(user, code: str) -> bool:
    """Check a TOTP code (no replay) or consume a recovery code."""
    with transaction.atomic():
        device = TotpDevice.objects.select_for_update().filter(user=user, confirmed_at__isnull=False).first()
        if device is None:
            return False
        secret = unseal(device.secret_sealed, purpose=PURPOSE)
        step = totp.verify(secret, code, last_used_step=device.last_used_step)
        if step is not None:
            device.last_used_step = step
            device.save(update_fields=["last_used_step"])
            return True
        recovery = RecoveryCode.objects.filter(user=user, code_hash=_hash(code), used_at__isnull=True).first()
        if recovery is not None:
            recovery.used_at = timezone.now()
            recovery.save(update_fields=["used_at"])
            record("auth.recovery_code_used", actor=user, entity="user", entity_id=user.pk)
            return True
    return False


def disable(user) -> None:
    with transaction.atomic():
        TotpDevice.objects.filter(user=user).delete()
        RecoveryCode.objects.filter(user=user).delete()
        record("auth.2fa_disabled", actor=user, entity="user", entity_id=user.pk)


def remaining_recovery_codes(user) -> int:
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
