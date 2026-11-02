"""CSV-Import und -Export für Kontakte und Leistungen.

Export and import use the same columns, so an export can be edited in a spreadsheet and read back.
Imports are all-or-nothing: every row is validated first; one bad row imports nothing and the
page lists each problem with its line number. Existing records are never changed – rows that
match an existing contact or item are skipped.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from skontio.audit.services import record
from skontio.catalog.models import Item, Unit
from skontio.crm.models import Customer
from skontio.crm.services import create_customer
from skontio.web.forms import TAX_TREATMENT_LABELS, CustomerForm, ItemForm

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_TAGS = 20

CUSTOMER_COLUMNS = [
    "Nummer", "Rolle", "Art", "Name", "USt-IdNr.", "E-Mail", "Telefon", "Straße", "PLZ", "Ort", "Land",
    "Sprache", "Leitweg-ID", "Zahlungsziel", "Tags", "Archiviert",
]  # fmt: skip
ITEM_COLUMNS = [
    "Artikelnummer", "Art", "Bezeichnung", "Beschreibung", "Einheit", "Nettopreis", "Steuer", "Arbeitskosten %",
    "Archiviert",
]  # fmt: skip

_COUNTRY = re.compile(r"^[A-Z]{2}$")
_FORMULA_ESCAPE = re.compile(r"^'(?=[=+\-@])")


class FileProblem(Exception):
    """The file itself is unusable (encoding, size, header)."""


@dataclass
class ImportResult:
    created: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---- export ------------------------------------------------------------------------------------


def customer_rows(company):
    for c in Customer.objects.filter(company=company).prefetch_related("tags").order_by("number"):
        yield [
            c.number, c.get_role_display(), c.get_kind_display(), c.name, c.vat_id, c.email, c.phone, c.street,
            c.postcode, c.city, c.country, c.language, c.buyer_reference,
            "" if c.payment_terms_days is None else c.payment_terms_days,
            ", ".join(sorted(t.name for t in c.tags.all())),
            c.archived_at.date() if c.archived_at else "",
        ]  # fmt: skip


def item_rows(company):
    for i in Item.objects.filter(company=company).select_related("unit").order_by("name", "pk"):
        yield [
            i.sku, i.get_kind_display(), i.name, i.description, i.unit.code, i.net_price, i.tax_treatment,
            i.labor_percent, i.archived_at.date() if i.archived_at else "",
        ]  # fmt: skip


def export_filename(kind: str) -> str:
    return f"{kind}-{timezone.localdate().isoformat()}.csv"


# ---- reading -----------------------------------------------------------------------------------


def read_table(data: bytes, required: set[str]) -> list[tuple[int, dict[str, str]]]:
    """(line number, row by header) for a semicolon/comma/tab separated file in UTF-8 or Windows-1252."""
    if len(data) > MAX_BYTES:
        raise FileProblem("Die Datei ist größer als 2 MB.")
    if b"\x00" in data:
        raise FileProblem("Das ist keine CSV-Datei.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    first = text.split("\n", 1)[0]
    delimiter = max((";", ",", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration as exc:
        raise FileProblem("Die Datei ist leer.") from exc
    except csv.Error as exc:
        raise FileProblem(f"Die Datei lässt sich nicht lesen: {exc}") from exc
    missing = required - set(header)
    if missing:
        raise FileProblem(f"Es fehlen Spalten: {', '.join(sorted(missing))}.")
    rows = []
    try:
        for values in reader:
            if not any(v.strip() for v in values):
                continue
            if len(rows) >= MAX_ROWS:
                raise FileProblem(f"Höchstens {MAX_ROWS} Zeilen auf einmal.")
            rows.append((reader.line_num, {h: _clean(v) for h, v in zip(header, values, strict=False) if h}))
    except csv.Error as exc:
        raise FileProblem(f"Zeile {reader.line_num}: {exc}") from exc
    if not rows:
        raise FileProblem("Die Datei enthält keine Datenzeilen.")
    return rows


def _clean(value: str) -> str:
    """Undo the formula escape of our own export ('=… → =…); the value is stored as plain text."""
    return _FORMULA_ESCAPE.sub("", value.strip())


def _choice(value: str, choices, default: str) -> str | None:
    """Accept the code or the German label, case-insensitive; empty → default; unknown → None."""
    if not value:
        return default
    wanted = value.casefold()
    for code, label in choices:
        if wanted in (str(code).casefold(), str(label).casefold()):
            return str(code)
    return None


def parse_amount(value: str) -> Decimal | None:
    """1234,50 · 1.234,50 · 1234.50 · 80 € → Decimal; anything else (also negative) → None."""
    raw = value.replace("€", "").replace(" ", "").replace(" ", "")
    if not re.fullmatch(r"\d{1,10}([.,]\d{3})*([.,]\d{1,4})?", raw):
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1 or re.search(r"\.\d{3}$", raw):
        raw = raw.replace(".", "")  # 1.234 or 1.234.567: German thousands
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount < Decimal("1e10") else None


def _form_errors(form) -> str:
    return "; ".join(
        f"{form.fields[name].label if name in form.fields else name}: {' '.join(errors)}"
        for name, errors in form.errors.items()
    )


# ---- contacts ----------------------------------------------------------------------------------


def import_customers(company, data: bytes, *, user=None) -> ImportResult:
    rows = read_table(data, {"Name"})
    result = ImportResult()
    existing = {
        (name.casefold(), postcode)
        for name, postcode in Customer.objects.filter(company=company).values_list("name", "postcode")
    }
    planned: list[tuple[dict, list[str]]] = []
    for line, row in rows:
        problems = []
        role = _choice(row.get("Rolle", ""), Customer.Role.choices, Customer.Role.CUSTOMER)
        kind = _choice(row.get("Art", ""), Customer.Kind.choices, Customer.Kind.ORGANISATION)
        country = (row.get("Land", "") or "DE").upper()
        if role is None:
            problems.append(f"Rolle „{row['Rolle'][:40]}“ unbekannt (Kunde, Lieferant, Kunde und Lieferant)")
        if kind is None:
            problems.append(f"Art „{row['Art'][:40]}“ unbekannt (Firma, Privatperson)")
        if not _COUNTRY.match(country):
            problems.append("Land muss ein ISO-Code aus zwei Buchstaben sein (z. B. DE)")
        form = CustomerForm(
            {
                "kind": kind or "", "role": role or "", "name": row.get("Name", ""), "street": row.get("Straße", ""),
                "postcode": row.get("PLZ", ""), "city": row.get("Ort", ""), "country": country,
                "email": row.get("E-Mail", ""), "phone": row.get("Telefon", ""), "vat_id": row.get("USt-IdNr.", ""),
                "buyer_reference": row.get("Leitweg-ID", ""), "language": row.get("Sprache", "") or "de",
                "payment_terms_days": row.get("Zahlungsziel", ""),
            }
        )  # fmt: skip
        if not form.is_valid():
            problems.append(_form_errors(form))
        tags = list(dict.fromkeys(t.strip()[:50] for t in row.get("Tags", "").split(",") if t.strip()))
        if len(tags) > MAX_TAGS:
            problems.append(f"höchstens {MAX_TAGS} Tags")
        if problems:
            result.errors.append(f"Zeile {line}: " + "; ".join(problems))
            continue
        fields = dict(form.cleaned_data)
        key = (fields["name"].strip().casefold(), fields["postcode"])
        if key in existing:
            result.skipped.append(f"Zeile {line}: {fields['name']} gibt es schon")
            continue
        existing.add(key)
        planned.append((fields, tags))
    if result.errors:
        return result
    with transaction.atomic():
        for fields, tags in planned:
            create_customer(company, name=fields.pop("name"), vat_id=fields.pop("vat_id"), tags=tags, **fields)
        result.created = len(planned)
        record(
            "contacts.imported", actor=user, company=company, entity="crm.Customer",
            data={"created": result.created, "skipped": len(result.skipped)},
        )  # fmt: skip
    return result


# ---- items -------------------------------------------------------------------------------------


def import_items(company, data: bytes, *, user=None) -> ImportResult:
    rows = read_table(data, {"Bezeichnung", "Einheit", "Nettopreis"})
    result = ImportResult()
    items = Item.objects.filter(company=company)
    skus = {s.casefold() for s in items.exclude(sku="").values_list("sku", flat=True)}
    names = {n.casefold() for n in items.filter(sku="").values_list("name", flat=True)}
    units = {}
    for unit in Unit.objects.all():
        units[unit.code.casefold()] = unit.code
        units[unit.label_de.casefold()] = unit.code
    treatments = [(t.value, label) for t, label in TAX_TREATMENT_LABELS.items()]
    planned = []
    for line, row in rows:
        problems = []
        kind = _choice(row.get("Art", ""), Item.Kind.choices, Item.Kind.SERVICE)
        treatment = _choice(row.get("Steuer", ""), treatments, "standard")
        unit = units.get(row.get("Einheit", "").casefold())
        price = parse_amount(row.get("Nettopreis", ""))
        labor = parse_amount(row.get("Arbeitskosten %", "") or "0")
        if labor is None or labor > 100:
            problems.append(f"Arbeitskosten „{row.get('Arbeitskosten %', '')[:20]}“ muss zwischen 0 und 100 liegen")
        if kind is None:
            problems.append(f"Art „{row['Art'][:40]}“ unbekannt (Leistung, Artikel)")
        if treatment is None:
            problems.append(f"Steuer „{row['Steuer'][:40]}“ unbekannt")
        if unit is None:
            problems.append(f"Einheit „{row.get('Einheit', '')[:40]}“ unbekannt")
        if price is None:
            problems.append(f"Nettopreis „{row.get('Nettopreis', '')[:40]}“ ist kein Betrag")
        form = ItemForm(
            {
                "kind": kind or "", "name": row.get("Bezeichnung", ""), "sku": row.get("Artikelnummer", ""),
                "description": row.get("Beschreibung", ""), "unit": unit or "",
                "net_price": str(price) if price is not None else "", "tax_treatment": treatment or "",
                "labor_percent": str(labor) if labor is not None else "",
            }
        )  # fmt: skip
        if not form.is_valid() and not problems:
            problems.append(_form_errors(form))
        if problems:
            result.errors.append(f"Zeile {line}: " + "; ".join(problems))
            continue
        cleaned = form.cleaned_data
        sku, name = cleaned["sku"].strip(), cleaned["name"].strip()
        if (sku and sku.casefold() in skus) or (not sku and name.casefold() in names):
            result.skipped.append(f"Zeile {line}: {sku or name} gibt es schon")
            continue
        (skus if sku else names).add((sku or name).casefold())
        planned.append(cleaned)
    if result.errors:
        return result
    with transaction.atomic():
        for cleaned in planned:
            Item.objects.create(company=company, **cleaned)
        result.created = len(planned)
        record(
            "items.imported", actor=user, company=company, entity="catalog.Item",
            data={"created": result.created, "skipped": len(result.skipped)},
        )  # fmt: skip
    return result
