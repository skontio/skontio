from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from skontio.catalog.models import Item, Unit
from skontio.tax.engine import TaxTreatment


def create_item(
    company,
    *,
    name: str,
    unit: str,
    net_price: Decimal,
    kind: Item.Kind = Item.Kind.SERVICE,
    tax_treatment: TaxTreatment = TaxTreatment.STANDARD,
    sku: str = "",
    description: str = "",
    category=None,
) -> Item:
    if net_price < 0:
        raise ValueError("Der Preis darf nicht negativ sein")
    try:
        unit_obj = Unit.objects.get(code=unit)
    except Unit.DoesNotExist as exc:
        raise ValueError(f"Unbekannte Einheit {unit!r}") from exc
    try:
        with transaction.atomic():
            return Item.objects.create(
                company=company,
                name=name.strip(),
                unit=unit_obj,
                net_price=net_price,
                kind=kind,
                tax_treatment=TaxTreatment(tax_treatment),
                sku=sku.strip(),
                description=description.strip(),
                category=category,
            )
    except IntegrityError as exc:
        raise ValueError(f"Artikelnummer {sku!r} ist bereits vergeben") from exc


def search_items(company, query: str = "", *, include_archived: bool = False) -> QuerySet[Item]:
    items = Item.objects.filter(company=company).select_related("unit")
    if not include_archived:
        items = items.filter(archived_at__isnull=True)
    if query.strip():
        q = query.strip()
        items = items.filter(Q(name__icontains=q) | Q(sku__iexact=q) | Q(description__icontains=q))
    return items.order_by("name")


def archive_item(item: Item) -> Item:
    """Items are archived, never deleted, once documents may reference them."""
    item.archived_at = timezone.now()
    item.save(update_fields=["archived_at"])
    return item
