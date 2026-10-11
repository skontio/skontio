"""Business documents (quotes, invoices, corrections).

Phase-0 shape: only what the lock and numbering spikes need. Snapshots, taxes and
totals follow in Phase 1a. Immutability is enforced twice: here in the ORM and by
database triggers (migration 0002), because raw SQL bypasses the ORM.
"""

from __future__ import annotations

from django.db import models


class DocumentLocked(Exception):
    """Raised when a finalized (festgeschriebenes) document would be changed."""


class Document(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Entwurf"
        FINALIZED = "finalized", "festgeschrieben"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="documents")
    doc_type = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    # NULL (not "") for drafts: the unique constraint must ignore unnumbered drafts
    number = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001
    issue_date = models.DateField()
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_type", "number"], name="uniq_document_number"),
            models.CheckConstraint(
                condition=models.Q(status="draft", number__isnull=True)
                | models.Q(status="finalized", number__isnull=False, locked_at__isnull=False),
                name="finalized_has_number",
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


class DocumentLine(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField()
    text = models.TextField()
    quantity = models.DecimalField(max_digits=14, decimal_places=4)
    unit_price = models.DecimalField(max_digits=14, decimal_places=4)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["document", "position"], name="uniq_line_position")]

    def __str__(self) -> str:
        return f"{self.document} · Pos. {self.position}"
