from django.conf import settings
from django.db import models


class OidcIdentity(models.Model):
    """Link between a local user and an OIDC subject. Matched by issuer + subject, never by e-mail."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oidc_identities")
    issuer = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["issuer", "subject"], name="uniq_oidc_identity")]
        verbose_name = "OIDC-Identität"
        verbose_name_plural = "OIDC-Identitäten"

    def __str__(self) -> str:
        return f"{self.issuer} · {self.subject}"


class TotpDevice(models.Model):
    """Authenticator app for the break-glass password login. The seed is stored sealed (AES-GCM)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="totp_device")
    secret_sealed = models.TextField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_step = models.BigIntegerField(null=True, blank=True)  # replay protection
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Authenticator-App"
        verbose_name_plural = "Authenticator-Apps"

    def __str__(self) -> str:
        return f"TOTP {self.user}"


class RecoveryCode(models.Model):
    """One-time fallback codes, stored as SHA-256 only (they are long random strings)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Wiederherstellungscode"
        verbose_name_plural = "Wiederherstellungscodes"

    def __str__(self) -> str:
        return f"Wiederherstellungscode {self.user}"
