"""Allocation of gap-free document numbers.

Concurrency: on Postgres the sequence row is locked with ``SELECT … FOR UPDATE``;
on SQLite every transaction starts with ``BEGIN IMMEDIATE`` (see settings), which
serialises writers for the whole database.
"""

from __future__ import annotations

import datetime as dt
import re

from django.db import connection

from skontio.numbering.models import NumberSequence

_TOKEN = re.compile(r"\{(YYYY|YY|MM|#+)\}")


class NumberingError(Exception):
    pass


def format_number(pattern: str, value: int, on_date: dt.date) -> str:
    counters = [m for m in _TOKEN.finditer(pattern) if m.group(1).startswith("#")]
    if len(counters) != 1:
        raise NumberingError(f"Muster {pattern!r} braucht genau einen Zähler {{#…}}")

    def substitute(match: re.Match[str]) -> str:
        placeholder = match.group(1)
        if placeholder == "YYYY":
            return f"{on_date.year:04d}"
        if placeholder == "YY":
            return f"{on_date.year % 100:02d}"
        if placeholder == "MM":
            return f"{on_date.month:02d}"
        return str(value).zfill(len(placeholder))

    result = _TOKEN.sub(substitute, pattern)
    if "{" in result or "}" in result:
        raise NumberingError(f"Muster {pattern!r} enthält unbekannte Platzhalter")
    return result


def allocate_number(company, doc_type: str, on_date: dt.date) -> str:
    """Return the next number. Must be called inside the transaction that persists it."""
    if not connection.in_atomic_block:
        raise NumberingError("allocate_number muss innerhalb von transaction.atomic() laufen")
    try:
        seq = NumberSequence.objects.select_for_update().get(company=company, doc_type=doc_type)
    except NumberSequence.DoesNotExist as exc:
        raise NumberingError(f"Kein Nummernkreis für {doc_type!r} angelegt") from exc

    year = on_date.year
    if seq.period_year is not None and year < seq.period_year and seq.reset == NumberSequence.Reset.YEARLY:
        raise NumberingError(
            f"Nummernkreis {doc_type!r} ist bereits im Jahr {seq.period_year}; "
            f"ein Dokument mit Datum {on_date.isoformat()} kann nicht mehr nummeriert werden"
        )
    if seq.reset == NumberSequence.Reset.YEARLY and seq.period_year != year:
        seq.next_value = 1
    seq.period_year = max(year, seq.period_year or year)

    value = seq.next_value
    seq.next_value = value + 1
    seq.save(update_fields=["period_year", "next_value"])
    return format_number(seq.pattern, value, on_date)
