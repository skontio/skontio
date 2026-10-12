from django.conf import settings
from django.db import models


class MailLog(models.Model):
    """Versandprotokoll: who sent which document version to whom, and whether it worked."""

    class Status(models.TextChoices):
        SENT = "sent", "Versendet"
        FAILED = "failed", "Fehlgeschlagen"

    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT, related_name="mail_logs")
    to = models.JSONField()
    cc = models.JSONField(default=list)
    subject = models.CharField(max_length=300)
    message_id = models.CharField(max_length=300, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices)
    error = models.TextField(blank=True, default="")
    attachments = models.JSONField(default=list)  # [{"name": ..., "sha256": ...}]
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]
        verbose_name = "Versandprotokoll"
        verbose_name_plural = "Versandprotokolle"

    def __str__(self) -> str:
        return f"{self.document} → {', '.join(self.to)} ({self.status})"
