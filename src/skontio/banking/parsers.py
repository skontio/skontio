"""Bank statement parsers: CAMT.053 (ISO 20022, every German bank), MT940 (SWIFT, older exports) and CSV
exports with column detection.

Both return ``Row`` objects with a signed amount (+ incoming, − outgoing). Uploaded files are
untrusted: the XML parser resolves no entities and loads nothing from the network; CSV values are
plain text and never evaluated.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from lxml import etree

MAX_ROWS = 20_000
MAX_AMOUNT = Decimal("1e12")


class StatementError(ValueError):
    pass


@dataclass(frozen=True)
class Row:
    booked_on: dt.date
    amount: Decimal
    currency: str
    counterparty: str
    iban: str
    purpose: str


# --- CAMT.053 ---------------------------------------------------------------------------------


def _text(element, path: str) -> str:
    found = element.xpath(path)
    return " ".join(str(f).strip() for f in found if str(f).strip()) if found else ""


def parse_camt053(data: bytes) -> list[Row]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise StatementError(f"CAMT-Datei nicht lesbar: {exc}") from exc
    rows = []
    for entry in root.xpath("//*[local-name()='Ntry']")[:MAX_ROWS]:
        raw = _text(entry, "./*[local-name()='Amt']/text()") or "0"
        if not re.fullmatch(r"\d{1,12}(\.\d{1,5})?", raw):  # ISO 20022 ActiveOrHistoricCurrencyAndAmount
            raise StatementError(f"Betrag nicht lesbar: {raw[:40]!r}")
        amount = Decimal(raw)
        currency = _text(entry, "./*[local-name()='Amt']/@Ccy") or "EUR"
        debit = _text(entry, "./*[local-name()='CdtDbtInd']/text()") == "DBIT"
        day = (
            _text(entry, "./*[local-name()='BookgDt']/*[local-name()='Dt']/text()")
            or _text(entry, "./*[local-name()='BookgDt']/*[local-name()='DtTm']/text()")[:10]
        )
        party = "Cdtr" if debit else "Dbtr"
        detail = (entry.xpath(".//*[local-name()='TxDtls']") or [entry])[0]
        counterparty = _text(
            detail, f".//*[local-name()='RltdPties']/*[local-name()='{party}']//*[local-name()='Nm']/text()"
        )
        iban = _text(
            detail, f".//*[local-name()='RltdPties']/*[local-name()='{party}Acct']//*[local-name()='IBAN']/text()"
        )
        purpose = _text(detail, ".//*[local-name()='RmtInf']/*[local-name()='Ustrd']/text()") or _text(
            entry, "./*[local-name()='AddtlNtryInf']/text()"
        )
        try:
            booked_on = dt.date.fromisoformat(day)
        except ValueError as exc:
            raise StatementError(f"Buchungsdatum fehlt oder ist ungültig: {day!r}") from exc
        signed = -amount if debit else amount
        rows.append(Row(booked_on, signed, currency, counterparty[:200], iban[:34], purpose[:500]))
    if not rows and not root.xpath("//*[local-name()='Stmt' or local-name()='Rpt']"):
        raise StatementError("Keine CAMT.053-Kontoauszugsdatei.")
    return rows


# --- CSV --------------------------------------------------------------------------------------

COLUMNS = {
    "date": ["buchungstag", "buchungsdatum", "buchung", "datum", "date", "booking date", "valutadatum", "wertstellung"],
    "amount": ["betrag (eur)", "betrag", "amount (eur)", "amount", "umsatz", "betrag in eur"],
    "credit": ["haben", "gutschrift", "eingang"],
    "debit": ["soll", "lastschrift", "ausgang"],
    "counterparty": [
        "beguenstigter/zahlungspflichtiger",
        "begünstigter/zahlungspflichtiger",
        "name zahlungsbeteiligter",
        "auftraggeber / begünstigter",
        "auftraggeber/empfänger",
        "zahlungsempfänger",
        "empfänger",
        "payee",
        "partner name",
        "name",
    ],
    "iban": ["kontonummer/iban", "iban zahlungsbeteiligter", "iban", "account number", "partner iban"],
    "purpose": ["verwendungszweck", "payment reference", "zweck", "buchungstext", "beschreibung", "description"],
    "currency": ["waehrung", "währung", "currency"],
}
DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise StatementError("Unbekannte Zeichenkodierung.")


def parse_amount(value: str) -> Decimal:
    value = value.strip().replace("€", "").replace("EUR", "").replace(" ", "").replace(" ", "")
    if not value:
        return Decimal("0")
    negative = value.startswith("-") or value.endswith("-") or value.startswith("(")
    value = value.strip("()+-")
    if "," in value and "." in value:
        german = value.rfind(",") > value.rfind(".")
        value = value.replace(".", "").replace(",", ".") if german else value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise StatementError(f"Betrag nicht lesbar: {value!r}") from exc
    # "NaN", "Infinity" and "1e99" are valid Decimals but no bank amount (the column holds 12 digits)
    if not amount.is_finite() or abs(amount) >= MAX_AMOUNT:
        raise StatementError(f"Betrag nicht lesbar: {value!r}")
    return -amount if negative else amount


def parse_date(value: str) -> dt.date:
    value = value.strip()[:10]
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).date()  # noqa: DTZ007 – a plain booking date
        except ValueError:
            continue
    raise StatementError(f"Datum nicht lesbar: {value!r}")


def _find(header: list[str], names: list[str]) -> int | None:
    normalized = [re.sub(r"\s+", " ", h.strip().strip('"').lower()) for h in header]
    for name in names:
        if name in normalized:
            return normalized.index(name)
    return None


def parse_csv(data: bytes) -> list[Row]:
    text = _decode(data)
    head = text.splitlines()[:20]
    delimiter = ";" if sum(line.count(";") for line in head) >= sum(line.count(",") for line in head) else ","
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as exc:
        raise StatementError(f"CSV-Datei nicht lesbar: {exc}") from exc
    for start, header in enumerate(records[:30]):  # noqa: B007 – used after the loop (header row)
        columns = {key: _find(header, names) for key, names in COLUMNS.items()}
        if columns["date"] is not None and (columns["amount"] is not None or columns["credit"] is not None):
            break
    else:
        raise StatementError("Keine Spalten für Datum und Betrag gefunden – bitte CAMT.053 oder CSV mit Kopfzeile.")

    def cell(record: list[str], key: str) -> str:
        index = columns[key]
        return record[index].strip() if index is not None and index < len(record) else ""

    rows = []
    for record in records[start + 1 : start + 1 + MAX_ROWS]:
        if not cell(record, "date"):
            continue  # empty lines and footers
        if columns["amount"] is not None:
            amount = parse_amount(cell(record, "amount"))
        else:
            amount = parse_amount(cell(record, "credit")) - abs(parse_amount(cell(record, "debit")))
        rows.append(
            Row(
                booked_on=parse_date(cell(record, "date")),
                amount=amount,
                currency=(cell(record, "currency") or "EUR")[:3].upper(),
                counterparty=cell(record, "counterparty")[:200],
                iban=cell(record, "iban").replace(" ", "")[:34],
                purpose=cell(record, "purpose")[:500],
            )
        )
    return rows


# --- MT940 ------------------------------------------------------------------------------------

_MT940_TAG = re.compile(r"^:(\d{2}[A-Z]?):", re.MULTILINE)
# :61: YYMMDD [MMDD] (C|D|RC|RD) [funds code] amount, then the type code (N…/F…/S…) and references
_MT940_LINE = re.compile(r"^(\d{6})(\d{4})?(RC|RD|C|D)([A-Z])?(\d{1,12}(?:,\d{0,2})?)(?=[NFS])")
_MT940_SUBFIELD = re.compile(r"\?(\d{2})")


def is_mt940(data: bytes) -> bool:
    head = data.lstrip()[:2000]
    return head.startswith((b":20:", b"{1:", b"\r\n:20:", b"\n:20:")) or (
        b"\n:61:" in data[:200_000] and b":20:" in head
    )


def _mt940_details(text: str) -> tuple[str, str, str]:
    """(counterparty, iban, purpose) from an :86: field – structured (?20…) or free text."""
    text = text.replace("\r", "").replace("\n", "")
    if not re.match(r"^\d{3}\?", text):
        return "", "", " ".join(text.split())
    parts: dict[str, list[str]] = {}
    pieces = _MT940_SUBFIELD.split(text[3:])
    for code, value in zip(pieces[1::2], pieces[2::2], strict=False):
        parts.setdefault(code, []).append(value.strip())
    purpose = "".join(v for code in sorted(parts) if "20" <= code <= "29" or "60" <= code <= "63" for v in parts[code])
    name = " ".join(v for code in ("32", "33") for v in parts.get(code, []) if v)
    iban = "".join(parts.get("31", [])).replace(" ", "")
    return name, iban if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban) else "", purpose


def parse_mt940(data: bytes) -> list[Row]:
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    fields: list[tuple[str, str]] = []
    matches = list(_MT940_TAG.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end]
        value = re.sub(r"\n-\}?\s*$", "", value.split("\n-}", 1)[0])  # statement separator "-" / "-}"
        fields.append((match.group(1), value.strip("\n")))
    if not any(tag == "61" for tag, _ in fields) and not any(tag == "20" for tag, _ in fields):
        raise StatementError("Keine MT940-Kontoauszugsdatei.")
    rows: list[Row] = []
    currency = "EUR"
    pending: dict | None = None

    def flush():
        if pending is not None:
            rows.append(Row(**pending))

    for tag, value in fields:
        if tag in ("60F", "60M"):
            currency = value[7:10] if len(value) >= 10 and value[7:10].isalpha() else currency
        elif tag == "61":
            flush()
            if len(rows) >= MAX_ROWS:
                pending = None
                break
            line = _MT940_LINE.match(value.replace("\n", ""))
            if not line:
                raise StatementError(f"Umsatzzeile nicht lesbar: {value[:40]!r}")
            day, _entry, mark, _funds, raw = line.groups()
            try:
                booked_on = dt.datetime.strptime(day, "%y%m%d").date()  # noqa: DTZ007 – a plain booking date
            except ValueError as exc:
                raise StatementError(f"Datum nicht lesbar: {day!r}") from exc
            amount = parse_amount(raw)
            negative = mark in ("D", "RC")  # RC: reversal of a credit
            pending = {"booked_on": booked_on, "amount": -amount if negative else amount, "currency": currency,
                       "counterparty": "", "iban": "", "purpose": ""}  # fmt: skip
        elif tag == "86" and pending is not None:
            name, iban, purpose = _mt940_details(value)
            pending |= {"counterparty": name[:200], "iban": iban[:34], "purpose": purpose[:500]}
    flush()
    return rows


def parse_statement(data: bytes) -> tuple[str, list[Row]]:
    if data.lstrip()[:1] == b"<":
        return "camt053", parse_camt053(data)
    if is_mt940(data):
        return "mt940", parse_mt940(data)
    return "csv", parse_csv(data)
