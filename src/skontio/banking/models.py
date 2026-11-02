from django.conf import settings
from django.db import models


class BankAccount(models.Model):
    """An own account (Girokonto, Tagesgeld, Kreditkarte …). Statements are assigned by their IBAN."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="bank_accounts")
    name = models.CharField("Bezeichnung", max_length=100)
    iban = models.CharField("IBAN", max_length=34, blank=True, default="")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "iban"], condition=~models.Q(iban=""), name="uniq_bank_account_iban"
            )
        ]
        verbose_name = "Bankkonto"
        verbose_name_plural = "Bankkonten"

    def __str__(self) -> str:
        return f"{self.name} ({self.iban[-4:]})" if self.iban else self.name


class BankTransaction(models.Model):
    """One line of an imported bank statement (CAMT.053, MT940 or CSV). Amount is signed: + incoming, − outgoing."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="bank_transactions")
    account = models.ForeignKey(
        BankAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="transactions"
    )
    booked_on = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    counterparty = models.CharField(max_length=200, blank=True, default="")
    iban = models.CharField(max_length=34, blank=True, default="")
    purpose = models.CharField(max_length=500, blank=True, default="")
    import_key = models.CharField(max_length=64)  # SHA-256 over the line + its repetition count
    source = models.CharField(max_length=20)  # "camt053" | "mt940" | "csv"
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


class BankRule(models.Model):
    """Zuordnungsregel: transactions whose counterparty/purpose contain the given text get a suggestion –
    create an expense of a category, or ignore (private). Applied only when a person clicks."""

    class Direction(models.TextChoices):
        OUT = "out", "Ausgänge"
        IN = "in", "Eingänge"
        ANY = "any", "beide"

    class Action(models.TextChoices):
        EXPENSE = "expense", "Ausgabe anlegen"
        IGNORE = "ignore", "privat/ignorieren"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="bank_rules")
    counterparty_contains = models.CharField("Gegenpartei enthält", max_length=100, blank=True, default="")
    purpose_contains = models.CharField("Verwendungszweck enthält", max_length=100, blank=True, default="")
    direction = models.CharField("Richtung", max_length=3, choices=Direction.choices, default=Direction.OUT)
    action = models.CharField("Dann", max_length=10, choices=Action.choices, default=Action.EXPENSE)
    category = models.CharField("Kategorie", max_length=20, blank=True, default="")
    supplier = models.CharField("Lieferant (für die Ausgabe)", max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        verbose_name = "Bankregel"
        verbose_name_plural = "Bankregeln"

    def __str__(self) -> str:
        parts = [f"„{self.counterparty_contains}“" if self.counterparty_contains else "",
                 f"Zweck „{self.purpose_contains}“" if self.purpose_contains else ""]  # fmt: skip
        return " und ".join(p for p in parts if p) + f" → {self.get_action_display()}"

    def matches(self, tx: BankTransaction) -> bool:
        if self.direction == self.Direction.OUT and tx.amount >= 0:
            return False
        if self.direction == self.Direction.IN and tx.amount <= 0:
            return False
        if not (self.counterparty_contains or self.purpose_contains):
            return False
        if self.counterparty_contains and self.counterparty_contains.casefold() not in tx.counterparty.casefold():
            return False
        return not (self.purpose_contains and self.purpose_contains.casefold() not in tx.purpose.casefold())
