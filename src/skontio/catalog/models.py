from django.db import models

from skontio.tax.engine import TaxTreatment


class Unit(models.Model):
    """Unit of measure, coded per UN/ECE Recommendation 20 (BT-130)."""

    code = models.CharField(max_length=8, unique=True)
    label_de = models.CharField(max_length=40)
    label_en = models.CharField(max_length=40)

    class Meta:
        ordering = ["label_de"]
        verbose_name = "Einheit"
        verbose_name_plural = "Einheiten"

    def __str__(self) -> str:
        return self.label_de


class ItemCategory(models.Model):
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="item_categories")
    name = models.CharField(max_length=100)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "parent", "name"], name="uniq_item_category")]
        verbose_name = "Kategorie"
        verbose_name_plural = "Kategorien"

    def __str__(self) -> str:
        return self.name


class Item(models.Model):
    """Leistung oder Artikel. The tax treatment is abstract so items survive a regime change."""

    class Kind(models.TextChoices):
        SERVICE = "service", "Leistung"
        GOOD = "good", "Artikel"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="items")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SERVICE)
    sku = models.CharField("Artikelnummer", max_length=40, blank=True, default="")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT)
    net_price = models.DecimalField(max_digits=14, decimal_places=4)
    tax_treatment = models.CharField(
        max_length=20, choices=[(t.value, t.name) for t in TaxTreatment], default=TaxTreatment.STANDARD
    )
    category = models.ForeignKey(ItemCategory, null=True, blank=True, on_delete=models.SET_NULL)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "sku"], condition=~models.Q(sku=""), name="uniq_item_sku_per_company"
            ),
            models.CheckConstraint(condition=models.Q(net_price__gte=0), name="item_price_not_negative"),
        ]
        verbose_name = "Leistung/Artikel"
        verbose_name_plural = "Leistungen/Artikel"

    def __str__(self) -> str:
        return self.name
