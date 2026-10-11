from django.db import models


class Company(models.Model):
    """A legal entity issuing documents. Every business table is scoped to one company."""

    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Firma"
        verbose_name_plural = "Firmen"

    def __str__(self) -> str:
        return self.name
