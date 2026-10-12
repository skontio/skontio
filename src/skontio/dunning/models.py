from decimal import Decimal

from django.conf import settings
from django.db import models


class DunningLevel(models.Model):
    """One step of the dunning process (Zahlungserinnerung, 1. Mahnung, …), configurable per company."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="dunning_levels")
    level = models.PositiveSmallIntegerField()
    name = models.CharField("Bezeichnung", max_length=60)
    days_after = models.PositiveSmallIntegerField("Tage nach Fälligkeit bzw. letzter Mahnung", default=7)
    pay_days = models.PositiveSmallIntegerField("Zahlungsfrist (Tage)", default=7)
    fee = models.DecimalField("Mahngebühr (€)", max_digits=8, decimal_places=2, default=Decimal("0"))
    text = models.TextField("Eigener Text", blank=True, default="")

    class Meta:
        ordering = ["company", "level"]
        constraints = [models.UniqueConstraint(fields=["company", "level"], name="uniq_dunning_level")]
        verbose_name = "Mahnstufe"
        verbose_name_plural = "Mahnstufen"

    def __str__(self) -> str:
        return f"{self.level}. {self.name}"


class DunningNotice(models.Model):
    """A reminder letter that was issued for an invoice. Archived as PDF, never changed."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="dunning_notices")
    invoice = models.ForeignKey("documents.Document", on_delete=models.PROTECT, related_name="dunning_notices")
    level = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=60)
    number = models.CharField(max_length=32)
    issued_on = models.DateField()
    pay_by = models.DateField()
    open_amount = models.DecimalField(max_digits=12, decimal_places=2)
    fee = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    language = models.CharField(max_length=5, default="de")
    blob = models.ForeignKey("archive.Blob", on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-issued_on", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["company", "number"], name="uniq_dunning_number"),
            models.UniqueConstraint(fields=["invoice", "level"], name="uniq_dunning_level_per_invoice"),
        ]
        verbose_name = "Mahnung"
        verbose_name_plural = "Mahnungen"

    def __str__(self) -> str:
        return f"{self.number} · {self.title}"

    @property
    def total(self) -> Decimal:
        return self.open_amount + self.fee
