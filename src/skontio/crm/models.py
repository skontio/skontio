from django.db import models


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
    language = models.CharField(max_length=5, default="de")
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
