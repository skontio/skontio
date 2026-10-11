from django.db import models


class NumberSequence(models.Model):
    """Configurable number range per company and document type.

    The counter only advances inside the transaction that finalizes a document,
    so a rollback never leaves a gap.
    """

    class Reset(models.TextChoices):
        YEARLY = "yearly", "jährlich"
        NEVER = "never", "nie"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="number_sequences")
    doc_type = models.CharField(max_length=32)
    pattern = models.CharField(max_length=64, help_text="Platzhalter: {YYYY} {YY} {MM} und genau ein {#…}")
    reset = models.CharField(max_length=8, choices=Reset.choices, default=Reset.YEARLY)
    period_year = models.PositiveIntegerField(null=True, blank=True)
    next_value = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "doc_type"], name="uniq_sequence_per_company_type")]
        verbose_name = "Nummernkreis"
        verbose_name_plural = "Nummernkreise"

    def __str__(self) -> str:
        return f"{self.company} · {self.doc_type} · {self.pattern}"
