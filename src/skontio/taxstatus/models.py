from django.conf import settings
from django.db import models

from skontio.tax.engine import Regime


class TaxStatusPeriod(models.Model):
    """Which VAT regime applies to services rendered in [valid_from, valid_to].

    Finalized documents carry their own regime snapshot; changing periods never
    rewrites them (plan §6.1).
    """

    class Reason(models.TextChoices):
        INITIAL = "initial", "Ersteinrichtung"
        THRESHOLD_PREVIOUS_YEAR = "threshold_prev_year", "Vorjahresgrenze überschritten"
        THRESHOLD_CURRENT_YEAR = "threshold_current_year", "Grenze im laufenden Jahr überschritten"
        VOLUNTARY_OPTION = "voluntary_option", "Verzicht auf die Kleinunternehmerregelung (§ 19 Abs. 3 UStG)"
        RETURN_TO_SMALL_BUSINESS = "return_small_business", "Rückkehr zur Kleinunternehmerregelung"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="tax_status_periods")
    regime = models.CharField(max_length=20, choices=[(r.value, r.name) for r in Regime])
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    reason = models.CharField(max_length=32, choices=Reason.choices)
    option_binding_until = models.DateField(null=True, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["company", "valid_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["company"], condition=models.Q(valid_to__isnull=True), name="one_open_tax_period"
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True) | models.Q(valid_to__gte=models.F("valid_from")),
                name="tax_period_valid_range",
            ),
        ]
        verbose_name = "Steuerstatus-Zeitraum"
        verbose_name_plural = "Steuerstatus-Zeiträume"

    def __str__(self) -> str:
        return f"{self.company} · {self.regime} ab {self.valid_from:%d.%m.%Y}"


class VatReturn(models.Model):
    """A Voranmeldung marked as submitted: the values are frozen so later changes become visible."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="vat_returns")
    year = models.PositiveSmallIntegerField()
    period = models.CharField(max_length=3)  # "M01".."M12" or "Q1".."Q4"
    values = models.JSONField()  # {"81": "1234", "66": "12.34", …}
    submitted_at = models.DateTimeField(auto_now_add=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["-year", "-period"]
        constraints = [models.UniqueConstraint(fields=["company", "year", "period"], name="uniq_vat_return")]
        verbose_name = "Umsatzsteuer-Voranmeldung"
        verbose_name_plural = "Umsatzsteuer-Voranmeldungen"

    def __str__(self) -> str:
        return f"USt-VA {self.year} {self.period}"
