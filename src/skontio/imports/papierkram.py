"""Umzug aus Papierkram: den CSV-Export (ZIP) übernehmen.

Übernommen werden Kontakte, Leistungen, Rechnungen und Angebote (als **Altbelege**: Originalnummer, Datum,
Beträge, eine Sammelposition – Positionen und PDFs enthält der CSV-Export nicht), Zahlungseingänge, Bankkonten
und Kontoumsätze. Was Skontio (noch) nicht kennt – Projekte, Zeiten, Reisekosten –, wird gezählt und gemeldet.

Alles oder nichts: läuft in einer Transaktion; jede Unstimmigkeit (Betrag passt nicht, Nummer schon vergeben)
bricht den Import ab, bevor etwas gespeichert ist. Ein zweiter Lauf mit demselben Export legt nichts doppelt an.
Der Export ist fremde Eingabe: Größen sind begrenzt, gelesen werden nur bekannte CSV-Dateien, nichts wird entpackt.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from django.db import transaction

from skontio.audit.services import record
from skontio.banking.models import BankAccount, BankTransaction
from skontio.catalog.models import Item
from skontio.catalog.services import create_item
from skontio.crm.models import Customer
from skontio.crm.services import CUSTOMER_SEQUENCE, create_customer, normalise_vat_id
from skontio.documents.models import Document
from skontio.documents.services import DocumentError, add_line, create_draft, seal_imported
from skontio.numbering.models import NumberSequence
from skontio.payments.models import Payment
from skontio.payments.services import PaymentError, paid_amount, record_payment
from skontio.tax.engine import Regime, TaxTreatment, money
from skontio.taxstatus.services import TaxStatusError, regime_for

SOURCE = "Papierkram"
NOTE = f"Übernommen aus {SOURCE}: nur Summen, Positionen und PDF liegen dort."
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_CSV_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50_000
SUPPORTED = {
    "Kontakte.csv", "Waren_oder_Dienstleistungen.csv", "Rechnungen.csv", "Angebote.csv", "Zahlungseingänge.csv",
    "Bankverbindungen.csv", "Kontoumsätze.csv",
}  # fmt: skip
NOT_SUPPORTED = {
    "Projekte.csv": "Projekte", "Zeiteinträge.csv": "Zeiteinträge", "Aufgaben.csv": "Aufgaben",
    "Reisekosten.csv": "Reisekosten", "Reisetage.csv": "Reisetage", "Belege.csv": "Belege (Ausgaben)",
    "Mahnungen.csv": "Mahnungen", "Lieferscheine.csv": "Lieferscheine",
}  # fmt: skip
COUNTRIES = {
    "deutschland": "DE", "österreich": "AT", "schweiz": "CH", "liechtenstein": "LI", "luxemburg": "LU",
    "niederlande": "NL", "belgien": "BE", "frankreich": "FR", "italien": "IT", "spanien": "ES", "polen": "PL",
    "tschechien": "CZ", "dänemark": "DK", "schweden": "SE", "vereinigtes königreich": "GB", "großbritannien": "GB",
    "vereinigte staaten": "US", "usa": "US", "irland": "IE", "portugal": "PT", "ungarn": "HU", "kroatien": "HR",
}  # fmt: skip
UNITS = {
    "stunde": "HUR", "stunden": "HUR", "std": "HUR", "std.": "HUR", "h": "HUR", "stück": "C62", "stk": "C62",
    "stk.": "C62", "tag": "DAY", "tage": "DAY", "monat": "MON", "monate": "MON", "jahr": "ANN", "jahre": "ANN",
    "woche": "WEE", "wochen": "WEE", "pauschale": "LS", "pauschal": "LS", "km": "KMT", "minute": "MIN",
    "minuten": "MIN",
}  # fmt: skip
ORG_WORDS = re.compile(
    r"(e\.\s?V\b|\b(GmbH|gGmbH|UG|AG|KG|OHG|GbR|PartG|e\.\s?K|SE|Ltd|Inc|Verein|Stiftung|Vertrieb|"
    r"Betrieb|Meisterbetrieb|Praxis|Kanzlei|Agentur|Studio|Service|Gesellschaft|Verband|Gemeinde|Schule)\b)",
    re.IGNORECASE,
)


class PapierkramError(ValueError):
    pass


@dataclass
class Report:
    created: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    not_imported: dict[str, int] = field(default_factory=dict)
    numbering: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class _Rollback(Exception):
    pass


# --- reading ---------------------------------------------------------------------------------------


def read_export(data: bytes) -> dict[str, list[dict[str, str]]]:
    if len(data) > MAX_ZIP_BYTES:
        raise PapierkramError("Der Export ist größer als 50 MB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PapierkramError("Das ist keine ZIP-Datei. Bitte den CSV-Export aus Papierkram hochladen.") from exc
    tables: dict[str, list[dict[str, str]]] = {}
    with archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename).name
            if info.is_dir() or name not in SUPPORTED | set(NOT_SUPPORTED):
                continue
            if info.file_size > MAX_CSV_BYTES:
                raise PapierkramError(f"{name} ist größer als 20 MB.")
            with archive.open(info) as handle:
                raw = handle.read(MAX_CSV_BYTES + 1)  # a lying header cannot unpack more than this
            if len(raw) > MAX_CSV_BYTES:
                raise PapierkramError(f"{name} ist größer als 20 MB.")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1252", errors="replace")
            rows = []
            for row in csv.DictReader(io.StringIO(text.replace("\x00", "")), delimiter=";"):
                if len(rows) >= MAX_ROWS:
                    raise PapierkramError(f"{name} hat mehr als {MAX_ROWS} Zeilen.")
                rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items() if isinstance(k, str)})
            tables[name] = rows
    if "Kontakte.csv" not in tables and "Rechnungen.csv" not in tables:
        raise PapierkramError("Im ZIP fehlen Kontakte.csv und Rechnungen.csv – ist das ein CSV-Export aus Papierkram?")
    return tables


def _amount(value: str) -> Decimal | None:
    raw = value.replace("€", "").replace(" ", "").strip()
    negative = raw.startswith("-")
    raw = raw.lstrip("+-")
    if not re.fullmatch(r"\d{1,10}(\.\d{3})*(,\d{1,4})?|\d{1,10}(\.\d{1,4})?", raw):
        return None
    if "," in raw or re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        raw = raw.replace(".", "").replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount >= Decimal("1e10"):
        return None
    return -amount if negative else amount


def _date(value: str) -> dt.date | None:
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value.strip()[:10], pattern).date()
        except ValueError:
            continue
    return None


def _country(value: str) -> str | None:
    value = value.strip()
    if not value:
        return "DE"
    if len(value) == 2 and value.isalpha():
        return value.upper()
    return COUNTRIES.get(value.lower())


def _kind(name: str) -> str:
    """Papierkram does not say whether a contact is a person; guess, the import report lists it."""
    if ORG_WORDS.search(name):
        return Customer.Kind.ORGANISATION
    words = name.split()
    if 2 <= len(words) <= 3 and all(w[:1].isupper() and w.replace("-", "").isalpha() for w in words):
        return Customer.Kind.PERSON
    return Customer.Kind.ORGANISATION


# --- import ----------------------------------------------------------------------------------------


def import_papierkram(
    company, data: bytes, *, user=None, dry_run: bool = False, continue_numbering: bool = True
) -> Report:
    """Import the export; with ``dry_run`` (or any error) nothing is saved, the report says what would happen."""
    tables = read_export(data)
    report = Report()
    for name, label in NOT_SUPPORTED.items():
        if tables.get(name):
            report.not_imported[label] = len(tables[name])
    try:
        with transaction.atomic():
            customers = _contacts(company, tables.get("Kontakte.csv", []), report)
            items = _items(company, tables.get("Waren_oder_Dienstleistungen.csv", []), report)
            invoices = _documents(
                company, tables.get("Rechnungen.csv", []), Document.Type.INVOICE, customers, items, report
            )
            _documents(company, tables.get("Angebote.csv", []), Document.Type.QUOTE, customers, items, report)
            payments = _payments(tables.get("Zahlungseingänge.csv", []), invoices, report, user)
            _paid_check(tables.get("Rechnungen.csv", []), invoices, report)
            _bank(company, tables.get("Bankverbindungen.csv", []), tables.get("Kontoumsätze.csv", []), payments, report)
            if continue_numbering:
                _continue_numbering(company, report)
            if report.errors or dry_run:
                raise _Rollback
            record("import.papierkram", actor=user, company=company,
                   data={"created": dict(report.created), "skipped": dict(report.skipped)})  # fmt: skip
    except _Rollback:
        pass
    return report


def _contacts(company, rows, report) -> dict[str, Customer]:
    by_number: dict[str, Customer] = {}
    for line, row in enumerate(rows, start=2):
        number, name = row.get("Kunden-/Lieferantennr.", "")[:32], " ".join(row.get("Name", "").split())[:200]
        if not name:
            report.warnings.append(f"Kontakte.csv Zeile {line}: ohne Namen übersprungen")
            continue
        existing = Customer.objects.filter(company=company, number=number).first() if number else None
        if existing is not None:
            if existing.name != name:
                report.errors.append(f"Kundennummer {number} gibt es schon für „{existing.name}“, nicht „{name}“")
            else:
                report.skipped["Kontakte"] += 1
                by_number[number] = existing
            continue
        country = _country(row.get("Land (Post)") or row.get("Land", ""))
        if country is None:
            report.warnings.append(f"{name}: Land „{row.get('Land (Post)')}“ unbekannt – als DE übernommen")
            country = "DE"
        try:
            vat_id = normalise_vat_id(row.get("UStID", ""))
        except ValueError:
            report.warnings.append(f"{name}: USt-IdNr. „{row.get('UStID')}“ ungültig – nicht übernommen")
            vat_id = ""
        kind_text = row.get("Kontaktart", "").lower()
        if "lieferant" in kind_text and "kunde" in kind_text:
            role = Customer.Role.BOTH
        elif "lieferant" in kind_text:
            role = Customer.Role.SUPPLIER
        else:
            role = Customer.Role.CUSTOMER
        email = row.get("E-Mail", "") or row.get("E-Mail (Standardempfänger)", "")
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            report.warnings.append(f"{name}: E-Mail „{email}“ ungültig – nicht übernommen")
            email = ""
        customer = create_customer(
            company, name=name, kind=_kind(name), role=role, vat_id=vat_id, country=country,
            street=row.get("Straße (Post)", "")[:200], postcode=row.get("PLZ (Post)", "")[:20],
            city=row.get("Stadt (Post)", "")[:100], email=email[:254],
            phone=(row.get("Telefon", "") or row.get("Telefonnummer (Standardempfänger)", ""))[:40],
        )  # fmt: skip
        if number:
            customer.number = number
            customer.save(update_fields=["number"])
        by_number[customer.number] = customer
        report.created["Kontakte"] += 1
        if customer.kind == Customer.Kind.PERSON:
            report.warnings.append(f"{name}: als Privatperson angelegt (geschätzt) – bei Bedarf in Kontakte ändern")
    return by_number


def _items(company, rows, report) -> dict[str, Item]:
    by_key: dict[str, Item] = {}
    for line, row in enumerate(rows, start=2):
        name, sku = " ".join(row.get("Name", "").split())[:200], row.get("Artikelnummer", "")[:64]
        if not name:
            continue
        lookup = {"sku": sku} if sku else {"name": name}
        existing = Item.objects.filter(company=company, **lookup).first()
        if existing is not None:
            report.skipped["Leistungen"] += 1
            by_key[sku or name] = existing
            continue
        price = _amount(row.get("Netto-Preis", ""))
        if price is None or price < 0:
            report.errors.append(
                f"Waren_oder_Dienstleistungen.csv Zeile {line}: Preis „{row.get('Netto-Preis')}“ unklar"
            )
            continue
        rate = _amount(row.get("USt.-Satz", "")) or Decimal("0")
        item = create_item(
            company, name=name, unit=UNITS.get(row.get("Einheitbezeichnung Einzel", "").strip().lower(), "C62"),
            net_price=price, sku=sku, description=row.get("Beschreibung", "")[:4000],
            kind=Item.Kind.GOOD if row.get("Typ", "").lower() in ("ware", "produkt", "artikel") else Item.Kind.SERVICE,
            tax_treatment=TaxTreatment.REDUCED if rate == 7 else TaxTreatment.STANDARD,
        )  # fmt: skip
        by_key[sku or name] = item
        report.created["Leistungen"] += 1
    return by_key


def lines_for(net: Decimal, vat: Decimal, regime: Regime) -> list[tuple[Decimal, TaxTreatment]] | None:
    """Split totals into lines per VAT rate (the CSV export has totals only). None = not representable."""
    if vat == 0:
        return [(net, TaxTreatment.STANDARD if regime is Regime.SMALL_BUSINESS else TaxTreatment.EXEMPT)]
    for rate, treatment in ((Decimal("19"), TaxTreatment.STANDARD), (Decimal("7"), TaxTreatment.REDUCED)):
        if money(net * rate / 100) == vat:
            return [(net, treatment)]
    full = ((vat - net * Decimal("0.07")) / Decimal("0.12")).quantize(Decimal("0.01"))
    reduced = net - full
    if full > 0 and reduced > 0 and money(full * Decimal("0.19")) + money(reduced * Decimal("0.07")) == vat:
        return [(full, TaxTreatment.STANDARD), (reduced, TaxTreatment.REDUCED)]
    return None


def _documents(company, rows, doc_type, customers, items, report) -> dict[str, Document]:
    invoice = doc_type == Document.Type.INVOICE
    label, singular = ("Rechnungen", "Rechnung") if invoice else ("Angebote", "Angebot")
    number_key = "Rechnungsnr." if invoice else "Angebots-Nr."
    imported: dict[str, Document] = {}
    for line, row in enumerate(rows, start=2):
        number, status = row.get(number_key, "")[:64], row.get("Status", "").lower()
        where = f"{singular} {number or f'Zeile {line}'}"
        if row.get("Typ", "") not in ("", singular):
            report.warnings.append(f"{where}: Typ „{row.get('Typ')}“ wird nicht übernommen")
            continue
        if "storniert" in status:
            report.warnings.append(f"{where}: storniert – nicht übernommen")
            continue
        draft = status == "entwurf" or number.lower() in ("", "entwurf")
        if draft and invoice:
            report.warnings.append(f"{where}: Entwurf – nicht übernommen")
            continue
        if not draft:
            existing = Document.objects.filter(company=company, doc_type=doc_type, number=number).first()
            if existing is not None:
                if existing.imported_from == SOURCE:
                    report.skipped[label] += 1
                    imported[number] = existing
                else:
                    report.errors.append(f"{where}: die Nummer gibt es in Skontio schon")
                continue
        elif Document.objects.filter(company=company, doc_type=doc_type, origin=Document.Origin.IMPORT,
                                     status=Document.Status.DRAFT, intro_text=_draft_marker(row)).exists():  # fmt: skip
            report.skipped[f"{label} (Entwurf)"] += 1
            continue
        customer = customers.get(row.get("Kundennummer", ""))
        day = _date(row.get("Datum", ""))
        net, vat, gross = (_amount(row.get(k, "")) for k in ("Netto", "USt.", "Brutto"))
        if customer is None or day is None or net is None or vat is None or gross is None or net < 0:
            report.errors.append(f"{where}: Kunde, Datum oder Betrag fehlt bzw. ist unklar")
            continue
        try:
            regime = regime_for(company, day)
        except TaxStatusError:
            report.errors.append(f"{where}: für {day:%d.%m.%Y} ist in Skontio kein Steuerstatus hinterlegt")
            continue
        parts = lines_for(net, vat, regime)
        if parts is None:
            report.errors.append(f"{where}: Netto {net} und USt {vat} passen zu keinem Steuersatz (19 %/7 %)")
            continue
        if vat == 0 and regime is not Regime.SMALL_BUSINESS:
            report.warnings.append(f"{where}: ohne Umsatzsteuer bei Regelbesteuerung – als steuerfrei übernommen")
        subject = " ".join(row.get("Betreff", "").split()) or f"Leistungen laut {singular} {number}"
        try:
            doc = create_draft(
                company, doc_type, customer, issue_date=day, service_date=day if invoice else None,
                due_date=_date(row.get("Fälligkeitsdatum", "")) if invoice else None, origin=Document.Origin.IMPORT,
                intro_text=_draft_marker(row) if draft else "",
            )  # fmt: skip
            item = items.get(row.get("Waren & Dienstleistungen", ""))
            for amount, treatment in parts:
                add_line(doc, text=subject[:300], quantity=Decimal("1"), unit_code="C62", unit_price=amount,
                         tax_treatment=treatment, item=item,
                         description=NOTE)  # fmt: skip
            if draft:
                report.created[f"{label} (Entwurf)"] += 1
                continue
            doc = seal_imported(doc, number=number, source=SOURCE)
        except DocumentError as exc:
            report.errors.append(f"{where}: {exc}")
            continue
        if doc.gross_total != gross:
            report.errors.append(f"{where}: Brutto in Skontio {doc.gross_total} statt {gross} – bitte prüfen")
            continue
        imported[number] = doc
        report.created[label] += 1
    return imported


def _draft_marker(row) -> str:
    """Drafts have no number: a stable marker in the intro text keeps a second import from adding them again."""
    basis = "|".join(row.get(k, "") for k in ("Kundennummer", "Datum", "Betreff", "Brutto"))
    return f"Entwurf aus {SOURCE} ({hashlib.sha256(basis.encode()).hexdigest()[:10]})"


def _payments(rows, invoices, report, user) -> list[Payment]:
    created = []
    note = f"Übernommen aus {SOURCE}"
    for line, row in enumerate(rows, start=2):
        number = row.get("Rechnungsnummer", "")
        day, amount = _date(row.get("Datum", "")), _amount(row.get("Betrag", ""))
        invoice = invoices.get(number)
        if invoice is None:
            report.warnings.append(f"Zahlungseingänge.csv Zeile {line}: Rechnung „{number}“ fehlt – übersprungen")
            continue
        if day is None or amount is None or amount <= 0:
            report.errors.append(f"Zahlungseingänge.csv Zeile {line}: Datum oder Betrag unklar")
            continue
        existing = Payment.objects.filter(document=invoice, amount=amount, received_on=day, note=note).first()
        if existing is not None:
            report.skipped["Zahlungen"] += 1
            created.append(existing)
            continue
        try:
            created.append(record_payment(invoice, amount, day, note=note, created_by=user))
        except PaymentError as exc:
            report.errors.append(f"Zahlung zu {number}: {exc}")
            continue
        report.created["Zahlungen"] += 1
    return created


def _paid_check(rows, invoices, report) -> None:
    for row in rows:
        invoice, paid = invoices.get(row.get("Rechnungsnr.", "")), _amount(row.get("Bezahlt", ""))
        if invoice is not None and paid is not None and paid_amount(invoice) != paid:
            report.warnings.append(
                f"Rechnung {invoice.number}: laut {SOURCE} {paid} bezahlt, in Skontio {paid_amount(invoice)}"
            )


def _bank(company, account_rows, rows, payments, report) -> None:
    accounts: dict[str, BankAccount] = {}
    for index, row in enumerate(account_rows, start=1):
        iban = re.sub(r"\s", "", row.get("IBAN", "")).upper()[:34]
        name = (row.get("Name", "") or f"Konto {index}")[:100]
        account = BankAccount.objects.filter(company=company, iban=iban, **({} if iban else {"name": name})).first()
        if account is None:
            account = BankAccount.objects.create(company=company, name=name, iban=iban)
            report.created["Bankkonten"] += 1
        accounts[str(index)] = account
    free = {p.pk: p for p in payments if not BankTransaction.objects.filter(payment=p).exists()}
    for line, row in enumerate(rows, start=2):
        if row.get("Storniert", "").lower() == "ja":
            continue
        day = _date(row.get("Buchungsdatum", "") or row.get("Valuta", ""))
        amount = _amount(row.get("Zahlungswert", ""))
        if day is None or amount is None:
            report.warnings.append(f"Kontoumsätze.csv Zeile {line}: Datum oder Betrag unklar – übersprungen")
            continue
        parts = (row.get("Bankverbindung-ID", ""), str(day), str(amount), row.get("Absender-Name", ""),
                 row.get("Verwendungszweck", ""), row.get("Instref", ""), row.get("Primanota", ""))  # fmt: skip
        key = hashlib.sha256(("papierkram|" + "|".join(parts)).encode()).hexdigest()
        if BankTransaction.objects.filter(company=company, import_key=key).exists():
            report.skipped["Kontoumsätze"] += 1
            continue
        tx = BankTransaction.objects.create(
            company=company, account=accounts.get(row.get("Bankverbindung-ID", "")), booked_on=day, amount=amount,
            currency=(row.get("Zahlungswährung") or "EUR")[:3], counterparty=row.get("Absender-Name", "")[:200],
            iban=re.sub(r"\s", "", row.get("Absender-IBAN", "")).upper()[:34],
            purpose=" ".join(row.get("Verwendungszweck", "").split())[:500], import_key=key, source="papierkram",
        )  # fmt: skip
        report.created["Kontoumsätze"] += 1
        matches = [p for p in free.values() if p.amount == amount and p.received_on == day] if amount > 0 else []
        if len(matches) == 1:  # the payment Papierkram booked for this transfer
            tx.payment = matches[0]
            tx.save(update_fields=["payment"])
            free.pop(matches[0].pk)


def _continue_numbering(company, report) -> None:
    """Continue Papierkram's number ranges (R-00006 after R-00005) – only while Skontio has none of its own."""

    def advance(key: str, numbers: list[str], label: str, *, set_pattern: bool) -> None:
        parsed = [m for m in (re.fullmatch(r"(\D*?)(\d+)", n) for n in numbers) if m]
        if not parsed:
            return
        formats = {(m.group(1), len(m.group(2))) for m in parsed}
        if len(formats) != 1:
            report.warnings.append(f"{label}: Nummern in mehreren Formaten – Nummernkreis bitte selbst prüfen")
            return
        ((prefix, width),) = formats
        pattern = f"{prefix}{{{'#' * width}}}"
        seq = NumberSequence.objects.filter(company=company, doc_type=key).first()
        if seq is None:
            seq = NumberSequence(company=company, doc_type=key, pattern=pattern, reset=NumberSequence.Reset.NEVER)
        elif set_pattern and seq.pattern != pattern:
            own = Document.objects.filter(
                company=company, doc_type=key, status=Document.Status.FINALIZED, imported_from=""
            ).exists()
            if own:
                report.warnings.append(f"{label}: Skontio vergibt schon eigene Nummern ({seq.pattern}) – bleibt so")
                return
            seq.pattern, seq.reset = pattern, NumberSequence.Reset.NEVER
        seq.next_value = max(seq.next_value or 1, max(int(m.group(2)) for m in parsed) + 1)
        seq.save()
        report.numbering.append(f"{label}: nächste Nummer {prefix}{str(seq.next_value).zfill(width)}")

    for doc_type, label in ((Document.Type.INVOICE, "Rechnungen"), (Document.Type.QUOTE, "Angebote")):
        numbers = Document.objects.filter(company=company, doc_type=doc_type, imported_from=SOURCE)
        advance(doc_type, list(numbers.values_list("number", flat=True)), label, set_pattern=True)
    numbers = list(Customer.objects.filter(company=company).values_list("number", flat=True))
    advance(CUSTOMER_SEQUENCE, numbers, "Kunden", set_pattern=False)
