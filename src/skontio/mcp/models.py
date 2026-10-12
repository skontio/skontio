from django.conf import settings
from django.db import models


class PersonalAccessToken(models.Model):
    """``skt_…`` token for MCP clients without OAuth (stdio bridge, Claude Code). Stored as SHA-256 only."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mcp_tokens")
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=16)  # shown in lists ("skt_ab12cd34…"), not secret
    token_hash = models.CharField(max_length=64, unique=True)
    scopes = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Persönlicher Zugangstoken"
        verbose_name_plural = "Persönliche Zugangstokens"

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @property
    def scope_list(self) -> list[str]:
        return self.scopes.split()
