from django.conf import settings
from django.db import models

from skontio.core.models import DOCUMENT_LANGUAGES


class Tag(models.Model):
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="tags")
    name = models.CharField(max_length=50)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "name"], name="uniq_tag_per_company")]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Customer(models.Model):
    class Kind(models.TextChoices):
        ORGANISATION = "org", "Firma"
        PERSON = "person", "Privatperson"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="customers")
    number = models.CharField(max_length=32)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ORGANISATION)
    name = models.CharField(max_length=200)
    vat_id = models.CharField("USt-IdNr.", max_length=20, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")
    street = models.CharField(max_length=200, blank=True, default="")
    postcode = models.CharField(max_length=20, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    country = models.CharField(max_length=2, default="DE")
    language = models.CharField("Sprache", max_length=5, choices=DOCUMENT_LANGUAGES, default="de")
    buyer_reference = models.CharField("Käuferreferenz/Leitweg-ID", max_length=100, blank=True, default="")
    payment_terms_days = models.PositiveSmallIntegerField(default=14)
    tags = models.ManyToManyField(Tag, blank=True, related_name="customers")
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uniq_customer_number")]
        verbose_name = "Kunde"
        verbose_name_plural = "Kunden"

    def __str__(self) -> str:
        return f"{self.number} {self.name}"


class Contact(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=100, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")

    class Meta:
        ordering = ["name"]
        verbose_name = "Ansprechpartner"
        verbose_name_plural = "Ansprechpartner"

    def __str__(self) -> str:
        return self.name


class Activity(models.Model):
    """What happened with a customer: call, e-mail, meeting, note. Free text – never exposed via MCP."""

    class Kind(models.TextChoices):
        NOTE = "note", "Notiz"
        CALL = "call", "Anruf"
        EMAIL = "email", "E-Mail"
        MEETING = "meeting", "Termin"

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="activities")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.NOTE)
    text = models.TextField()
    occurred_at = models.DateTimeField()
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.SET_NULL, related_name="activities"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        verbose_name = "Aktivität"
        verbose_name_plural = "Aktivitäten"

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.customer}"


class FollowUp(models.Model):
    """Wiedervorlage: something to do for a customer by a date (e.g. follow up on a quote)."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="follow_ups")
    title = models.CharField(max_length=200)
    due_on = models.DateField()
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.SET_NULL, related_name="follow_ups"
    )
    done_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_on", "pk"]
        indexes = [models.Index(fields=["done_at", "due_on"])]
        verbose_name = "Wiedervorlage"
        verbose_name_plural = "Wiedervorlagen"

    def __str__(self) -> str:
        return f"{self.title} ({self.due_on:%d.%m.%Y})"
