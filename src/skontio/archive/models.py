from django.db import models


class Blob(models.Model):
    """A content-addressed, write-once file in the archive (GoBD: Unveränderbarkeit, Aufbewahrung)."""

    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveBigIntegerField()
    mime = models.CharField(max_length=100)
    storage_key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    retention_until = models.DateField(null=True, blank=True)
    legal_hold = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Archivdatei"
        verbose_name_plural = "Archivdateien"

    def __str__(self) -> str:
        return f"{self.sha256[:12]} ({self.mime}, {self.size} B)"
