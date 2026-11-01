from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class LedgerSettings(models.Model):
    """Chart of accounts and DATEV identifiers of a company (agree them with the tax advisor)."""

    class Chart(models.TextChoices):
        SKR03 = "skr03", "SKR 03"
        SKR04 = "skr04", "SKR 04"

    company = models.OneToOneField("core.Company", on_delete=models.PROTECT, related_name="ledger_settings")
    chart = models.CharField("Kontenrahmen", max_length=5, choices=Chart.choices, default=Chart.SKR03)
    account_length = models.PositiveSmallIntegerField(
        "Sachkontenlänge", default=4, validators=[MinValueValidator(4), MaxValueValidator(8)]
    )
    consultant_number = models.PositiveIntegerField(
        "Beraternummer", null=True, blank=True, validators=[MinValueValidator(1001), MaxValueValidator(9999999)]
    )
    client_number = models.PositiveIntegerField(
        "Mandantennummer", null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(99999)]
    )

    class Meta:
        verbose_name = "Buchhaltungseinstellung"
        verbose_name_plural = "Buchhaltungseinstellungen"

    def __str__(self) -> str:
        return f"{self.get_chart_display()} · Berater {self.consultant_number or '–'}"


class AccountAssignment(models.Model):
    """Own account for one booking purpose (e.g. "income_standard"), overriding the chart's suggestion."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="account_assignments")
    key = models.CharField(max_length=40)
    account = models.CharField(max_length=8)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "key"], name="uniq_account_assignment")]
        verbose_name = "Kontozuordnung"
        verbose_name_plural = "Kontozuordnungen"

    def __str__(self) -> str:
        return f"{self.key} → {self.account}"
