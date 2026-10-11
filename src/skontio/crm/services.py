from __future__ import annotations

import re

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from skontio.crm.models import Customer, Tag
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import allocate_number

CUSTOMER_SEQUENCE = "customer"
DEFAULT_CUSTOMER_PATTERN = "K-{#####}"

_VAT_ID = re.compile(r"^[A-Z]{2}[A-Z0-9+*]{2,12}$")
_VAT_ID_DE = re.compile(r"^DE\d{9}$")


def normalise_vat_id(raw: str) -> str:
    """Syntactic check only; online validation (BZSt/VIES) follows later."""
    vat_id = re.sub(r"[\s.\-]", "", raw).upper()
    if not vat_id:
        return ""
    if not _VAT_ID.match(vat_id) or (vat_id.startswith("DE") and not _VAT_ID_DE.match(vat_id)):
        raise ValueError(f"{raw!r} ist keine gültige USt-IdNr.")
    return vat_id


def create_customer(company, *, name: str, tags: list[str] | None = None, vat_id: str = "", **fields) -> Customer:
    with transaction.atomic():
        NumberSequence.objects.get_or_create(
            company=company,
            doc_type=CUSTOMER_SEQUENCE,
            defaults={"pattern": DEFAULT_CUSTOMER_PATTERN, "reset": NumberSequence.Reset.NEVER},
        )
        customer = Customer.objects.create(
            company=company,
            number=allocate_number(company, CUSTOMER_SEQUENCE, timezone.localdate()),
            name=name.strip(),
            vat_id=normalise_vat_id(vat_id),
            **fields,
        )
        for tag_name in tags or []:
            tag, _ = Tag.objects.get_or_create(company=company, name=tag_name.strip())
            customer.tags.add(tag)
    return customer


def search_customers(company, query: str = "", *, include_archived: bool = False) -> QuerySet[Customer]:
    customers = Customer.objects.filter(company=company)
    if not include_archived:
        customers = customers.filter(archived_at__isnull=True)
    if query.strip():
        q = query.strip()
        customers = customers.filter(
            Q(name__icontains=q) | Q(number__iexact=q) | Q(city__icontains=q) | Q(email__icontains=q)
        )
    return customers.order_by("name")
