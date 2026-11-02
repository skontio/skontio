from __future__ import annotations

from typing import Annotated

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from pydantic import AfterValidator, Field, StringConstraints

from skontio.crm.models import Customer
from skontio.crm.services import create_customer, search_customers
from skontio.tools.inputs import CustomerNumber, Input, Limit, Name, Query
from skontio.tools.registry import Out, ToolError, tool
from skontio.tools.views import customer_view

Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


def _email(value: str) -> str:
    if value:
        try:
            validate_email(value)
        except ValidationError:
            raise ValueError("keine gültige E-Mail-Adresse") from None
    return value


Email = Annotated[str, StringConstraints(strip_whitespace=True, max_length=254), AfterValidator(_email)]


def find_customer(ctx, number: str) -> Customer:
    customer = Customer.objects.filter(company=ctx.company, number=number, archived_at__isnull=True).first()
    if customer is None:
        raise ToolError(f"Kunde {number!r} nicht gefunden – customer_search liefert gültige Kundennummern")
    return customer


class SearchInput(Input):
    query: Query = ""
    limit: Limit = 20


@tool(name="customer_search", title="Kunden suchen", scope="customers:read", input_model=SearchInput)
def customer_search(ctx, args: SearchInput) -> Out:
    """Sucht Kunden nach Name oder Kundennummer. Liefert nur Kundennummer, Name, Art und Land;
    Privatpersonen erscheinen im Pseudonym-Modus als „Privatperson <Kundennummer>“."""
    customers = search_customers(ctx.company, args.query)[: args.limit]
    return Out({"customers": [customer_view(c, ctx) for c in customers]})


class GetInput(Input):
    customer_number: CustomerNumber


@tool(name="customer_get", title="Kunde abrufen", scope="customers:read", input_model=GetInput)
def customer_get(ctx, args: GetInput) -> Out:
    """Liefert einen Kunden mit Zahlungsziel und ob die Anschrift für eine Rechnung vollständig ist.
    Kontaktdaten und Anschrift selbst werden nie herausgegeben."""
    customer = find_customer(ctx, args.customer_number)
    return Out(customer_view(customer, ctx, detail=True), "customer", customer.pk)


class CreateInput(Input):
    kind: Customer.Kind = Customer.Kind.ORGANISATION
    name: Name
    street: Short = ""
    postcode: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] = ""
    city: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] = "DE"
    vat_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] = ""
    email: Email = ""
    buyer_reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    payment_terms_days: Annotated[int, Field(ge=0, le=120)] | None = None  # None: company default


@tool(
    name="customer_create",
    title="Kunde anlegen",
    scope="customers:write",
    input_model=CreateInput,
    read_only=False,
    idempotent=False,
)
def customer_create(ctx, args: CreateInput) -> Out:
    """Legt einen Kunden an und vergibt die Kundennummer. Für Rechnungen ist die vollständige
    Anschrift nötig (Straße, PLZ, Ort)."""
    customer = create_customer(ctx.company, **args.model_dump())
    return Out(customer_view(customer, ctx, detail=True), "customer", customer.pk)


class UpdateInput(Input):
    customer_number: CustomerNumber
    name: Name | None = None
    kind: Customer.Kind | None = None
    street: Short | None = None
    postcode: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None
    city: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    vat_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None
    email: Email | None = None
    buyer_reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    payment_terms_days: Annotated[int, Field(ge=0, le=120)] | None = None


@tool(
    name="customer_update",
    title="Kunde ändern",
    scope="customers:write",
    input_model=UpdateInput,
    read_only=False,
    idempotent=True,
)
def customer_update(ctx, args: UpdateInput) -> Out:
    """Ändert Stammdaten eines Kunden (nur angegebene Felder). Festgeschriebene Rechnungen behalten die
    Anschrift von damals; die Änderung gilt für neue Dokumente."""
    from skontio.audit.services import record
    from skontio.crm.services import normalise_vat_id

    customer = find_customer(ctx, args.customer_number)
    changes = args.model_dump(exclude={"customer_number"}, exclude_none=True)
    if "vat_id" in changes:
        try:
            changes["vat_id"] = normalise_vat_id(changes["vat_id"])
        except ValueError as exc:
            raise ToolError(str(exc)) from None
    for field, value in changes.items():
        setattr(customer, field, value)
    customer.save()
    record("customer.update", company=ctx.company, entity="customer", entity_id=customer.pk,
           data={"fields": sorted(changes)})  # fmt: skip
    return Out(customer_view(customer, ctx, detail=True), "customer", customer.pk)
