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


MAX_NEXT_VALUE = 10**9


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """Regex matching every number the pattern can produce; groups ``year``, ``yy`` and ``n``."""
    parts, pos = [], 0
    for match in _TOKEN.finditer(pattern):
        parts.append(re.escape(pattern[pos : match.start()]))
        placeholder = match.group(1)
        if placeholder == "YYYY":
            parts.append(r"(?P<year>\d{4})")
        elif placeholder == "YY":
            parts.append(r"(?P<yy>\d{2})")
        elif placeholder == "MM":
            parts.append(r"\d{2}")
        else:
            parts.append(rf"(?P<n>\d{{{len(placeholder)},}})")
        pos = match.end()
    parts.append(re.escape(pattern[pos:]))
    return re.compile("^" + "".join(parts) + "$")


def change_problems(
    seq: NumberSequence,
    *,
    pattern: str,
    reset: str,
    next_value: int,
    existing: list[str],
    today: dt.date,
    max_length: int = 32,
) -> list[str]:
    """Why a change would risk duplicate or confusing numbers; empty when it is safe."""
    problems = []
    if reset not in NumberSequence.Reset.values:
        return ["Unbekannte Rücksetzung."]
    if not 1 <= next_value <= MAX_NEXT_VALUE:
        return [f"Die nächste Nummer muss zwischen 1 und {MAX_NEXT_VALUE:,} liegen.".replace(",", ".")]
    try:
        preview = format_number(pattern, next_value, today)
    except NumberingError as exc:
        return [str(exc)]
    if len(pattern) > 64 or len(format_number(pattern, MAX_NEXT_VALUE, today)) > max_length:
        problems.append(f"Nummern dürfen höchstens {max_length} Zeichen lang werden.")
    if reset == NumberSequence.Reset.YEARLY and not re.search(r"\{YY(YY)?\}", pattern):
        problems.append(
            "Bei jährlicher Rücksetzung braucht das Muster {YYYY} oder {YY} – sonst wiederholen sich Nummern."
        )
    issued = bool(existing)  # numbers were really handed out (a configured start value alone does not count)
    unchanged = seq.pk is not None and pattern == seq.pattern and reset == seq.reset
    if issued and unchanged and next_value < seq.next_value:
        problems.append(f"Bereits vergebene Nummern: Die nächste Nummer darf nicht unter {seq.next_value} sinken.")
    regex = pattern_regex(pattern)
    for number in existing:
        match = regex.match(number or "")
        if not match:
            continue
        year = match.groupdict().get("year")
        yy = match.groupdict().get("yy")
        same_period = reset == NumberSequence.Reset.NEVER or (
            (year is None or int(year) == today.year) and (yy is None or int(yy) == today.year % 100)
        )
        if same_period and int(match.group("n")) >= next_value:
            problems.append(f"Die Nummer {number} gibt es schon – die nächste Nummer muss größer sein.")
            break
    if not problems and preview in existing:
        problems.append(f"Die Nummer {preview} gibt es schon.")
    return problems


def update_sequence(
    seq: NumberSequence,
    *,
    pattern: str,
    reset: str,
    next_value: int,
    existing: list[str],
    today: dt.date,
    user=None,
    max_length: int = 32,
) -> NumberSequence:
    from django.db import transaction

    from skontio.audit.services import record

    pattern = pattern.strip()
    with transaction.atomic():
        if seq.pk is not None:
            seq = NumberSequence.objects.select_for_update().get(pk=seq.pk)
        problems = change_problems(
            seq,
            pattern=pattern,
            reset=reset,
            next_value=next_value,
            existing=existing,
            today=today,
            max_length=max_length,
        )
        if problems:
            raise NumberingError(" ".join(problems))
        before = {"pattern": seq.pattern, "reset": seq.reset, "next_value": seq.next_value}
        seq.pattern, seq.reset, seq.next_value = pattern, reset, next_value
        # anchor the period, otherwise the next allocation would reset a configured start value to 1
        seq.period_year = max(seq.period_year or today.year, today.year)
        seq.save()
        record(
            "numbering.changed", actor=user, company=seq.company, entity="number_sequence", entity_id=seq.pk,
            data={"doc_type": seq.doc_type, "before": before,
                  "after": {"pattern": pattern, "reset": reset, "next_value": next_value}},
        )  # fmt: skip
    return seq
