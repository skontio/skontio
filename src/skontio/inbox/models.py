from django.conf import settings
from django.db import models


class IncomingInvoice(models.Model):
    """A received e-invoice (Empfangspflicht seit 2025). The original file stays in the archive unchanged."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="incoming_invoices")
    original = models.ForeignKey("archive.Blob", on_delete=models.PROTECT, related_name="+")
    xml = models.ForeignKey("archive.Blob", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    filename = models.CharField(max_length=255, blank=True, default="")
    syntax = models.CharField(max_length=8)  # cii | ubl
    number = models.CharField(max_length=100, blank=True, default="")
    seller_name = models.CharField(max_length=300, blank=True, default="")
    seller_vat_id = models.CharField(max_length=30, blank=True, default="")
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default="")
    net_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    gross_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    data = models.JSONField(default=dict)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [models.UniqueConstraint(fields=["company", "original"], name="uniq_incoming_per_file")]
        verbose_name = "Eingangsrechnung"
        verbose_name_plural = "Eingangsrechnungen"

    def __str__(self) -> str:
        return f"{self.seller_name} {self.number}".strip()
