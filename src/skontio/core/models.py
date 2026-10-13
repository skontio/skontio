from django.conf import settings
from django.db import models

# Languages of what customers receive (PDF, e-invoice notes, e-mail); the UI stays German
DOCUMENT_LANGUAGES = [("de", "Deutsch"), ("en", "Englisch")]


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
    # Umsatzsteuer-Voranmeldung (only relevant under Regelbesteuerung or for § 13b)
    vat_period = models.CharField(
        "Voranmeldungszeitraum",
        max_length=10,
        choices=[("quarterly", "vierteljährlich"), ("monthly", "monatlich")],
        default="quarterly",
    )
    vat_basis = models.CharField(
        "Besteuerung nach",
        max_length=4,
        choices=[("ist", "vereinnahmten Entgelten (Ist)"), ("soll", "vereinbarten Entgelten (Soll)")],
        default="ist",
    )
    vat_extension = models.BooleanField("Dauerfristverlängerung", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Firma"
        verbose_name_plural = "Firmen"

    def __str__(self) -> str:
        return self.name


class ProcedureDocument(models.Model):
    """One version of the Verfahrensdokumentation (GoBD Rz. 151 ff.), archived as PDF for 10 years."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="procedure_documents")
    version = models.PositiveIntegerField()
    blob = models.ForeignKey("archive.Blob", on_delete=models.PROTECT, related_name="+")
    note = models.CharField("Anlass der Änderung", max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["-version"]
        constraints = [models.UniqueConstraint(fields=["company", "version"], name="uniq_procedure_version")]
        verbose_name = "Verfahrensdokumentation"
        verbose_name_plural = "Verfahrensdokumentationen"

    def __str__(self) -> str:
        return f"Verfahrensdokumentation v{self.version}"


class JobRun(models.Model):
    """One scheduled background job per day (``skontio worker``): prevents double runs after restarts."""

    class Status(models.TextChoices):
        RUNNING = "running", "läuft"
        OK = "ok", "erfolgreich"
        FAILED = "failed", "fehlgeschlagen"

    job = models.CharField(max_length=40)
    day = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    attempts = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-day", "job"]
        constraints = [models.UniqueConstraint(fields=["job", "day"], name="uniq_job_per_day")]

    def __str__(self) -> str:
        return f"{self.job} {self.day:%d.%m.%Y}: {self.get_status_display()}"
