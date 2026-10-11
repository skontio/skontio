from django.conf import settings
from django.db import models


class PaymentError(ValueError):
    pass


class PaymentLocked(PaymentError):
    """Payments are bookings: correct them with a reversal, never by deleting or editing."""


class Payment(models.Model):
    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Überweisung"
        CASH = "cash", "Bar"
        PAYPAL = "paypal", "PayPal"
        OTHER = "other", "Sonstiges"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="payments")
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # negative only for reversals
    received_on = models.DateField()
    method = models.CharField(max_length=16, choices=Method.choices, default=Method.BANK_TRANSFER)
    note = models.CharField(max_length=300, blank=True, default="")
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["received_on", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(reverses__isnull=True, amount__gt=0) | models.Q(reverses__isnull=False, amount__lt=0)
                ),
                name="payment_sign_matches_kind",
            )
        ]
        verbose_name = "Zahlung"
        verbose_name_plural = "Zahlungen"

    def __str__(self) -> str:
        return f"{self.received_on:%d.%m.%Y} {self.amount} € → {self.document}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise PaymentLocked("Zahlungen werden nicht geändert, sondern storniert")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PaymentLocked("Zahlungen werden nicht gelöscht, sondern storniert")
