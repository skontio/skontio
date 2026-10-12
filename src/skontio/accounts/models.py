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
