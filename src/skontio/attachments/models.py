from django.conf import settings
from django.db import models


class Attachment(models.Model):
    """A file filed with a customer and/or a document (contract, photo, order, correspondence)."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="attachments")
    customer = models.ForeignKey(
        "crm.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="attachments"
    )
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="attachments"
    )
    blob = models.ForeignKey("archive.Blob", on_delete=models.PROTECT, related_name="attachments")
    filename = models.CharField(max_length=200)
    mime = models.CharField(max_length=100)
    note = models.CharField(max_length=500, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-pk"]
        verbose_name = "Datei"
        verbose_name_plural = "Dateien"

    def __str__(self) -> str:
        return self.filename
