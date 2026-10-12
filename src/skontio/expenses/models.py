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
    supplier = models.CharField("Lieferant", max_length=200)
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

    class Meta:
        ordering = ["-uploaded_at", "-pk"]
        verbose_name = "Offener Beleg"
        verbose_name_plural = "Offene Belege"

    def __str__(self) -> str:
        return self.filename
