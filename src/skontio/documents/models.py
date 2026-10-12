"""Business documents: quotes, order confirmations, invoices and cancellations (Storno).

Immutability is enforced twice: here in the ORM and by database triggers
(``skontio.documents.locking``), because raw SQL bypasses the ORM. Personal data of the
parties lives in ``PartySnapshot`` – deliberately *not* locked – so it can be redacted once
the retention period has ended while the locked document keeps the SHA-256 of the
original (ADR 0001, option b).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from skontio.tax.engine import Regime, TaxTreatment


class DocumentLocked(Exception):
    """Raised when a finalized (festgeschriebenes) document would be changed."""


class Document(models.Model):
    class Type(models.TextChoices):
        QUOTE = "quote", "Angebot"
        ORDER_CONFIRMATION = "order_confirmation", "Auftragsbestätigung"
        INVOICE = "invoice", "Rechnung"
        PARTIAL_INVOICE = "partial_invoice", "Abschlagsrechnung"
        FINAL_INVOICE = "final_invoice", "Schlussrechnung"
        CANCELLATION = "cancellation", "Stornorechnung"

    class Status(models.TextChoices):
        DRAFT = "draft", "Entwurf"
        FINALIZED = "finalized", "festgeschrieben"

    class Origin(models.TextChoices):
        UI = "ui", "Oberfläche"
        MCP = "mcp", "MCP"
        ASSISTANT = "assistant", "KI-Assistent"
        API = "api", "API"
        RECURRING = "recurring", "Wiederkehrend"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="documents")
    doc_type = models.CharField(max_length=32, choices=Type.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    # NULL (not "") for drafts: the unique constraint must ignore unnumbered drafts
    number = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.PROTECT)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")

    issue_date = models.DateField()
    service_date = models.DateField(null=True, blank=True)
    service_period_start = models.DateField(null=True, blank=True)
    service_period_end = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="EUR")
    language = models.CharField(max_length=5, default="de")
    intro_text = models.TextField(blank=True, default="")
    closing_text = models.TextField(blank=True, default="")
    payment_terms_text = models.TextField(blank=True, default="")
    document_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cancellation_reason = models.TextField(blank=True, default="")

    # snapshot written when finalizing
    regime = models.CharField(max_length=20, blank=True, default="", choices=[(r.value, r.name) for r in Regime])
    tax_note = models.TextField(blank=True, default="")
    net_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    gross_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    seller_snapshot_sha256 = models.CharField(max_length=64, blank=True, default="")
    buyer_snapshot_sha256 = models.CharField(max_length=64, blank=True, default="")

    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.UI)
    ai_review_required = models.BooleanField(default=False)
    ai_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    ai_reviewed_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=100, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issue_date", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_type", "number"], name="uniq_document_number"),
            models.CheckConstraint(
                condition=models.Q(status="draft", number__isnull=True)
                | models.Q(status="finalized", number__isnull=False, locked_at__isnull=False),
                name="finalized_has_number",
            ),
            models.UniqueConstraint(
                fields=["parent"], condition=models.Q(doc_type="cancellation"), name="one_cancellation_per_document"
            ),
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_document_idempotency_key",
            ),
        ]
        verbose_name = "Dokument"
        verbose_name_plural = "Dokumente"

    def __str__(self) -> str:
        return self.number or f"Entwurf #{self.pk}"

    def save(self, *args, **kwargs):
        if self.is_locked:
            raise DocumentLocked(f"Dokument {self.number} ist festgeschrieben")
        super().save(*args, **kwargs)
        self._stored_status = self.status

    def delete(self, *args, **kwargs):
        if self.is_locked:
            raise DocumentLocked(f"Dokument {self.number} ist festgeschrieben")
        return super().delete(*args, **kwargs)

    @classmethod
    def from_db(cls, db, field_names, values, *args, **kwargs):
        instance = super().from_db(db, field_names, values, *args, **kwargs)
        instance._stored_status = instance.status
        return instance

    @property
    def is_locked(self) -> bool:
        return getattr(self, "_stored_status", self.Status.DRAFT) != self.Status.DRAFT


# Everything that demands payment: § 14 UStG duties, due date, payments, e-invoice, 8-year retention
INVOICE_TYPES = frozenset({Document.Type.INVOICE, Document.Type.PARTIAL_INVOICE, Document.Type.FINAL_INVOICE})


class DocumentLine(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField()
    item = models.ForeignKey("catalog.Item", null=True, blank=True, on_delete=models.PROTECT)
    text = models.TextField()
    description = models.TextField(blank=True, default="")
    quantity = models.DecimalField(max_digits=14, decimal_places=4)
    unit_code = models.CharField(max_length=8, default="C62")
    unit_price = models.DecimalField(max_digits=14, decimal_places=4)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_treatment = models.CharField(
        max_length=20, choices=[(t.value, t.name) for t in TaxTreatment], default=TaxTreatment.STANDARD
    )
    # snapshot written when finalizing
    tax_category = models.CharField(max_length=4, blank=True, default="")
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    tax_exemption_reason = models.TextField(blank=True, default="")
    net_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["document", "position"], name="uniq_line_position")]

    def __str__(self) -> str:
        return f"{self.document} · Pos. {self.position}"


class PrepaymentDeduction(models.Model):
    """Abschlagsrechnung deducted in a Schlussrechnung, per VAT category (§ 14 Abs. 5 Satz 2 UStG).

    Amounts are taken from the finalized partial invoice; locked together with the final invoice.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="deductions")
    source = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="deducted_in")
    tax_category = models.CharField(max_length=4)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2)
    net_amount = models.DecimalField(max_digits=14, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["source__issue_date", "source_id", "tax_category", "tax_rate"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "source", "tax_category", "tax_rate"], name="uniq_deduction_per_category"
            )
        ]

    def __str__(self) -> str:
        return f"{self.document} · Abzug {self.source}"

    @property
    def gross_amount(self):
        return self.net_amount + self.tax_amount


class Rendition(models.Model):
    """Archived output of a finalized document (PDF/A, ZUGFeRD PDF, XRechnung XML)."""

    class Kind(models.TextChoices):
        PDF = "pdf", "PDF/A"
        ZUGFERD_PDF = "zugferd_pdf", "ZUGFeRD-PDF (EN 16931)"
        XRECHNUNG_XML = "xrechnung_xml", "XRechnung (CII)"

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="renditions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    blob = models.ForeignKey("archive.Blob", on_delete=models.PROTECT, related_name="renditions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "kind"], name="uniq_rendition_kind")]

    def __str__(self) -> str:
        return f"{self.document} · {self.kind}"


class PartySnapshot(models.Model):
    """Seller/buyer data as printed on a finalized document. Not locked: redactable after retention."""

    class Role(models.TextChoices):
        SELLER = "seller", "Verkäufer"
        BUYER = "buyer", "Käufer"

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="party_snapshots")
    role = models.CharField(max_length=8, choices=Role.choices)
    data = models.JSONField()
    sha256 = models.CharField(max_length=64)
    redacted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "role"], name="uniq_party_snapshot_role")]

    def __str__(self) -> str:
        return f"{self.document} · {self.role}"
