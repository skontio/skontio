from decimal import Decimal

from django.conf import settings
from django.db import models

from skontio.core.models import DOCUMENT_LANGUAGES
from skontio.tax.engine import TaxTreatment


class RecurringInvoice(models.Model):
    """Abo/Wartungsvertrag: creates an invoice draft per period. Never finalizes or sends by itself."""

    class Interval(models.IntegerChoices):
        MONTHLY = 1, "monatlich"
        QUARTERLY = 3, "vierteljährlich"
        HALF_YEARLY = 6, "halbjährlich"
        YEARLY = 12, "jährlich"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="recurring_invoices")
    customer = models.ForeignKey("crm.Customer", on_delete=models.PROTECT, related_name="recurring_invoices")
    name = models.CharField("Bezeichnung", max_length=120)
    interval = models.PositiveSmallIntegerField("Rhythmus", choices=Interval.choices, default=Interval.MONTHLY)
    start_date = models.DateField("Erster Leistungszeitraum ab")
    end_date = models.DateField("Endet am", null=True, blank=True)
    in_advance = models.BooleanField("Im Voraus abrechnen", default=True)
    generated = models.PositiveIntegerField(default=0)  # number of periods created so far
    active = models.BooleanField("Aktiv", default=True)
    language = models.CharField(max_length=5, choices=DOCUMENT_LANGUAGES, default="de")
    template = models.ForeignKey(
        "documents.DocumentTemplate", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    intro_text = models.TextField("Einleitung", blank=True, default="")
    closing_text = models.TextField("Schlusstext", blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "pk"]
        verbose_name = "Wiederkehrende Rechnung"
        verbose_name_plural = "Wiederkehrende Rechnungen"

    def __str__(self) -> str:
        return self.name


class RecurringLine(models.Model):
    recurring = models.ForeignKey(RecurringInvoice, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField()
    item = models.ForeignKey("catalog.Item", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    text = models.CharField(max_length=300)
    description = models.TextField(blank=True, default="")
    quantity = models.DecimalField(max_digits=14, decimal_places=4)
    unit_code = models.CharField(max_length=8)
    unit_price = models.DecimalField(max_digits=14, decimal_places=4)
    tax_treatment = models.CharField(max_length=20, choices=[(t.value, t.name) for t in TaxTreatment])
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["recurring", "position"]

    def __str__(self) -> str:
        return self.text


class RecurringRun(models.Model):
    """One generated period – the unique constraint makes generation idempotent."""

    recurring = models.ForeignKey(RecurringInvoice, on_delete=models.PROTECT, related_name="runs")
    period_start = models.DateField()
    period_end = models.DateField()
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        constraints = [
            models.UniqueConstraint(fields=["recurring", "period_start"], name="uniq_recurring_period"),
        ]

    def __str__(self) -> str:
        return f"{self.recurring} ab {self.period_start:%d.%m.%Y}"
