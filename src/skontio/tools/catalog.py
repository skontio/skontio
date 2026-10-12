from __future__ import annotations

from pydantic import model_validator

from skontio.catalog.models import Item
from skontio.catalog.services import create_item, search_items, update_item
from skontio.tax.engine import TaxTreatment
from skontio.tools.inputs import SKU, Input, Limit, Money, Name, Query, Text, UnitCode
from skontio.tools.registry import Out, ToolError, tool
from skontio.tools.views import item_view


def _item(ctx, item_id: int) -> Item:
    try:
        return Item.objects.select_related("unit").get(company=ctx.company, pk=item_id)
    except Item.DoesNotExist:
        raise ToolError(f"Leistung {item_id} nicht gefunden – catalog_search liefert gültige item_id") from None


class SearchInput(Input):
    query: Query = ""
    include_archived: bool = False
    limit: Limit = 20


@tool(name="catalog_search", title="Leistungen suchen", scope="catalog:read", input_model=SearchInput)
def catalog_search(ctx, args: SearchInput) -> Out:
    """Sucht im Leistungskatalog nach Bezeichnung, Artikelnummer oder Beschreibung.
    Liefert item_id, Preis (netto), Einheit und Steuerbehandlung für Rechnungspositionen."""
    items = search_items(ctx.company, args.query, include_archived=args.include_archived)
    return Out({"items": [item_view(i) for i in items[: args.limit]]})


class GetInput(Input):
    item_id: int


@tool(name="catalog_get", title="Leistung abrufen", scope="catalog:read", input_model=GetInput)
def catalog_get(ctx, args: GetInput) -> Out:
    """Liefert eine Leistung aus dem Katalog."""
    return Out(item_view(_item(ctx, args.item_id)), "item", args.item_id)


class CreateInput(Input):
    name: Name
    unit: UnitCode
    net_price: Money
    tax_treatment: TaxTreatment = TaxTreatment.STANDARD
    kind: Item.Kind = Item.Kind.SERVICE
    sku: SKU = ""
    description: Text = ""


@tool(name="catalog_create", title="Leistung anlegen", scope="catalog:write", input_model=CreateInput, read_only=False)
def catalog_create(ctx, args: CreateInput) -> Out:
    """Legt eine Leistung im Katalog an. net_price ist der Nettopreis je Einheit; die Umsatzsteuer
    ergibt sich erst auf der Rechnung aus Steuerstatus und Leistungsdatum."""
    item = create_item(ctx.company, **args.model_dump())
    return Out(item_view(item), "item", item.pk)


class UpdateInput(Input):
    item_id: int
    name: Name | None = None
    unit: UnitCode | None = None
    net_price: Money | None = None
    tax_treatment: TaxTreatment | None = None
    sku: SKU | None = None
    description: Text | None = None

    @model_validator(mode="after")
    def something_to_change(self):
        if not self.model_dump(exclude_none=True, exclude={"item_id"}):
            raise ValueError("Mindestens ein Feld zum Ändern angeben")
        return self


@tool(
    name="catalog_update",
    title="Leistung ändern",
    scope="catalog:write",
    input_model=UpdateInput,
    read_only=False,
    idempotent=True,
)
def catalog_update(ctx, args: UpdateInput) -> Out:
    """Ändert eine Leistung. Bereits erstellte Dokumente bleiben unverändert."""
    item = update_item(_item(ctx, args.item_id), **args.model_dump(exclude_none=True, exclude={"item_id"}))
    return Out(item_view(item), "item", item.pk)
