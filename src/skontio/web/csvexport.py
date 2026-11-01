"""CSV downloads for spreadsheets: semicolons, German decimals, UTF-8 with BOM, formula-safe text."""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterable
from decimal import Decimal

from django.http import HttpResponse

_FORMULA_START = {"=", "+", "-", "@", "\t", "\r"}


def csv_safe(value: str) -> str:
    """Spreadsheet formula injection: a leading =, +, - or @ would be executed by Excel/LibreOffice."""
    return "'" + value if value[:1] in _FORMULA_START else value


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.2f}".replace(".", ",")
    if isinstance(value, dt.date):
        return value.isoformat()
    return csv_safe(str(value))


def csv_response(filename: str, header: list[str], rows: Iterable[Iterable]) -> HttpResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([cell(v) for v in row])
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
