from django.conf import settings
from django.db import models


class BankTransaction(models.Model):
    """One line of an imported bank statement (CAMT.053 or CSV). Amount is signed: + incoming, − outgoing."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="bank_transactions")
    booked_on = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    counterparty = models.CharField(max_length=200, blank=True, default="")
    iban = models.CharField(max_length=34, blank=True, default="")
    purpose = models.CharField(max_length=500, blank=True, default="")
    import_key = models.CharField(max_length=64)  # SHA-256 over the line + its repetition count
    source = models.CharField(max_length=20)  # "camt053" | "csv"
    payment = models.OneToOneField(
        "payments.Payment", null=True, blank=True, on_delete=models.PROTECT, related_name="bank_transaction"
    )
    expense = models.ForeignKey(
        "expenses.Expense", null=True, blank=True, on_delete=models.PROTECT, related_name="bank_transactions"
    )
    ignored = models.BooleanField(default=False)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-booked_on", "-pk"]
        constraints = [models.UniqueConstraint(fields=["company", "import_key"], name="uniq_bank_import")]
        verbose_name = "Bankumsatz"
        verbose_name_plural = "Bankumsätze"

    def __str__(self) -> str:
        return f"{self.booked_on:%d.%m.%Y} {self.amount} {self.counterparty}"

    @property
    def done(self) -> bool:
        return self.ignored or self.payment_id is not None or self.expense_id is not None
