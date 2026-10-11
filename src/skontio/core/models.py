from django.db import models


class Company(models.Model):
    """A legal entity issuing documents. Every business table is scoped to one company."""

    class Activity(models.TextChoices):
        TRADE = "trade", "Gewerbe"
        FREELANCE = "freelance", "Freiberuflich"

    name = models.CharField(max_length=200)
    activity = models.CharField(max_length=10, choices=Activity.choices, default=Activity.TRADE)
    side_business = models.BooleanField("Nebenerwerb", default=False)
    street = models.CharField(max_length=200, blank=True, default="")
    postcode = models.CharField(max_length=20, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    country = models.CharField(max_length=2, default="DE")
    tax_number = models.CharField("Steuernummer", max_length=30, blank=True, default="")
    vat_id = models.CharField("USt-IdNr.", max_length=20, blank=True, default="")
    seller_identifier = models.CharField("Verkäuferkennung (BT-29)", max_length=50, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")
    contact_name = models.CharField(max_length=200, blank=True, default="")
    iban = models.CharField("IBAN", max_length=34, blank=True, default="")
    bic = models.CharField("BIC", max_length=11, blank=True, default="")
    account_holder = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Firma"
        verbose_name_plural = "Firmen"

    def __str__(self) -> str:
        return self.name
