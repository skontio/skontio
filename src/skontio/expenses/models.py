from decimal import Decimal

from django.conf import settings
from django.db import models


class Expense(models.Model):
    """A business expense with its receipt (Beleg). Never deleted – cancelled with a reason (GoBD)."""

    class Category(models.TextChoices):
        # grouped like the expense section of the Anlage EÜR
        GOODS = "goods", "Waren, Roh- und Hilfsstoffe"
        SUBCONTRACTING = "subcontracting", "Fremdleistungen"
        RENT = "rent", "Miete, Nebenkosten (Geschäftsräume)"
        TELECOM = "telecom", "Telefon, Internet"
        SOFTWARE = "software", "Software, Lizenzen, Hosting"
        GWG = "gwg", "Geringwertige Wirtschaftsgüter (bis 800 € netto)"
        OFFICE = "office", "Büromaterial, Porto"
        TRAVEL = "travel", "Reisekosten"
        VEHICLE = "vehicle", "Fahrzeugkosten"
        TRAINING = "training", "Fortbildung, Fachliteratur"
        ADVERTISING = "advertising", "Werbung"
        INSURANCE = "insurance", "Versicherungen, Beiträge"
        ADVISORY = "advisory", "Rechts- und Steuerberatung, Buchführung"
        FEES = "fees", "Kontoführung, Gebühren"
        HOSPITALITY = "hospitality", "Bewirtung (70 % abziehbar)"
        OTHER = "other", "Sonstige Betriebsausgaben"

    class Method(models.TextChoices):
        BANK = "bank", "Überweisung/Lastschrift"
        CARD = "card", "Karte"
        CASH = "cash", "Bar"
        PRIVATE = "private", "Privat bezahlt"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="expenses")
    number = models.CharField("Belegnummer", max_length=32)
    receipt_date = models.DateField("Belegdatum")
    paid_on = models.DateField("Bezahlt am", null=True, blank=True)  # Abfluss (EÜR)
    supplier = models.CharField("Lieferant", max_length=200)  # as on the receipt (kept even if the contact changes)
    supplier_contact = models.ForeignKey(
        "crm.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="expenses",
        verbose_name="Lieferant (Kontakt)",
    )  # fmt: skip
    supplier_country = models.CharField("Land des Lieferanten", max_length=2, default="DE")
    description = models.CharField("Beschreibung", max_length=300, blank=True, default="")
    category = models.CharField("Kategorie", max_length=20, choices=Category.choices, default=Category.OTHER)
    gross_amount = models.DecimalField("Betrag (brutto)", max_digits=12, decimal_places=2)
    vat_rate = models.DecimalField("USt-Satz", max_digits=5, decimal_places=2, default=Decimal("19"))
    reverse_charge = models.BooleanField("Steuerschuldnerschaft des Leistungsempfängers (§ 13b)", default=False)
    method = models.CharField("Zahlart", max_length=10, choices=Method.choices, default=Method.BANK)
    receipt = models.ForeignKey("archive.Blob", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    receipt_filename = models.CharField(max_length=200, blank=True, default="")
    note = models.CharField("Notiz", max_length=500, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-receipt_date", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["company", "number"], name="uniq_expense_number"),
            models.CheckConstraint(condition=models.Q(gross_amount__gt=0), name="expense_amount_positive"),
        ]
        verbose_name = "Ausgabe"
        verbose_name_plural = "Ausgaben"

    def __str__(self) -> str:
        return f"{self.number} · {self.supplier}"


class PendingReceipt(models.Model):
    """Belegeingang: a receipt shared from the phone or uploaded in bulk, waiting to become an expense."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="pending_receipts")
    blob = models.ForeignKey("archive.Blob", on_delete=models.PROTECT, related_name="+")
    filename = models.CharField(max_length=200)
    note = models.CharField(max_length=500, blank=True, default="")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    expense = models.OneToOneField(
        Expense, null=True, blank=True, on_delete=models.PROTECT, related_name="pending_receipt"
    )
    # Belegerkennung: checked fields suggested by the language model (expenses.recognition), never saved unseen
    recognized = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-uploaded_at", "-pk"]
        verbose_name = "Offener Beleg"
        verbose_name_plural = "Offene Belege"

    def __str__(self) -> str:
        return self.filename


class ReceiptMail(models.Model):
    """A mail read from the Beleg-Postfach – recorded once per Message-ID, so it is never imported twice."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="receipt_mails")
    message_id = models.CharField(max_length=255)
    sender = models.CharField(max_length=320, blank=True, default="")
    subject = models.CharField(max_length=300, blank=True, default="")
    result = models.CharField(max_length=500, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["company", "message_id"], name="uniq_receipt_mail")]
        verbose_name = "Beleg-Mail"
        verbose_name_plural = "Beleg-Mails"

    def __str__(self) -> str:
        return f"{self.sender}: {self.subject}"


class OtherIncome(models.Model):
    """Sonstige Einnahme ohne Rechnung aus Skontio (platform payout, cash sale, refund, interest …).

    Counts in EÜR, Umsatzsteuer and DATEV like a payment. Never deleted – cancelled with a reason.
    """

    class Category(models.TextChoices):
        PLATFORM = "platform", "Auszahlung einer Plattform"
        SALE = "sale", "Verkauf ohne Rechnung (z. B. bar)"
        INTEREST = "interest", "Zinsen"
        ASSET_SALE = "asset_sale", "Verkauf von Anlagevermögen"
        REFUND = "refund", "Erstattung"
        SUBSIDY = "subsidy", "Zuschuss"
        OTHER = "other", "Sonstige Einnahme"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="other_income")
    number = models.CharField("Belegnummer", max_length=32)
    received_on = models.DateField("Erhalten am")  # Zufluss (EÜR, Istversteuerung)
    payer = models.CharField("Von", max_length=200)
    description = models.CharField("Beschreibung", max_length=300, blank=True, default="")
    category = models.CharField("Art", max_length=20, choices=Category.choices, default=Category.OTHER)
    gross_amount = models.DecimalField("Betrag (brutto)", max_digits=12, decimal_places=2)
    vat_rate = models.DecimalField("USt-Satz", max_digits=5, decimal_places=2, default=Decimal("0"))
    method = models.CharField("Zahlart", max_length=10, choices=Expense.Method.choices, default=Expense.Method.BANK)
    receipt = models.ForeignKey("archive.Blob", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    receipt_filename = models.CharField(max_length=200, blank=True, default="")
    note = models.CharField("Notiz", max_length=500, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-received_on", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["company", "number"], name="uniq_other_income_number"),
            models.CheckConstraint(condition=models.Q(gross_amount__gt=0), name="other_income_amount_positive"),
        ]
        verbose_name = "Sonstige Einnahme"
        verbose_name_plural = "Sonstige Einnahmen"

    def __str__(self) -> str:
        return f"{self.number} · {self.payer}"


class RecurringExpense(models.Model):
    """Wiederkehrende Ausgabe (Miete, Abo): a suggestion per period, never booked without a person.

    The due date of the next period is ``start_date + done × interval`` months; taking it over opens a
    prefilled expense form, skipping it just counts it as done.
    """

    class Interval(models.IntegerChoices):
        MONTHLY = 1, "monatlich"
        QUARTERLY = 3, "vierteljährlich"
        HALF_YEARLY = 6, "halbjährlich"
        YEARLY = 12, "jährlich"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="recurring_expenses")
    name = models.CharField("Bezeichnung", max_length=120)
    supplier = models.CharField("Lieferant", max_length=200)
    supplier_contact = models.ForeignKey(
        "crm.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="recurring_expenses"
    )
    supplier_country = models.CharField(max_length=2, default="DE")
    description = models.CharField(max_length=300, blank=True, default="")
    category = models.CharField(max_length=20, choices=Expense.Category.choices, default=Expense.Category.OTHER)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("19"))
    reverse_charge = models.BooleanField(default=False)
    method = models.CharField(max_length=10, choices=Expense.Method.choices, default=Expense.Method.BANK)
    interval = models.PositiveSmallIntegerField("Rhythmus", choices=Interval.choices, default=Interval.MONTHLY)
    start_date = models.DateField("Erster Termin")
    end_date = models.DateField("Endet am", null=True, blank=True)
    done = models.PositiveIntegerField(default=0)  # periods taken over or skipped
    active = models.BooleanField(default=True)
    source = models.ForeignKey(Expense, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(gross_amount__gt=0), name="recurring_expense_amount_positive")
        ]
        verbose_name = "Wiederkehrende Ausgabe"
        verbose_name_plural = "Wiederkehrende Ausgaben"

    def __str__(self) -> str:
        return self.name
