"""Hash-chained audit log (GoBD: Nachvollziehbarkeit; plan §5 AuditEvent).

Each event stores the hash of its predecessor. Changing or deleting an event breaks the chain,
which ``skontio verify_audit`` reports. UPDATE is refused by a database trigger.
Free-text requests from AI clients are not stored in clear text, only their SHA-256 digest.
"""

from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    class ActorKind(models.TextChoices):
        USER = "user", "Person"
        MCP = "mcp", "MCP-Client"
        ASSISTANT = "assistant", "KI-Assistent"
        API = "api", "API"
        SYSTEM = "system", "System"

    seq = models.PositiveBigIntegerField(unique=True)
    created_at = models.DateTimeField()
    company = models.ForeignKey("core.Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_kind = models.CharField(max_length=16, choices=ActorKind.choices)
    client_id = models.CharField(max_length=255, blank=True, default="")
    tool_name = models.CharField(max_length=100, blank=True, default="")
    ai_initiated = models.BooleanField(default=False)
    action = models.CharField(max_length=100)
    entity = models.CharField(max_length=100, blank=True, default="")
    entity_id = models.CharField(max_length=64, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    request_digest = models.CharField(max_length=64, blank=True, default="")
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["-seq"]
        indexes = [models.Index(fields=["entity", "entity_id"])]
        verbose_name = "Protokolleintrag"
        verbose_name_plural = "Änderungsprotokoll"

    def __str__(self) -> str:
        return f"#{self.seq} {self.action}"


class AuditHead(models.Model):
    """Single row holding the end of the chain; locked while appending so the chain stays linear."""

    seq = models.PositiveBigIntegerField(default=0)
    hash = models.CharField(max_length=64, default="0" * 64)

    class Meta:
        verbose_name = "Protokollende"

    def __str__(self) -> str:
        return f"Protokollende #{self.seq}"
