"""MT940: Umsätze mit Vorzeichen, Storno-Kennzeichen, strukturierter Verwendungszweck, Müll und Doppelte."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from skontio.banking.models import BankTransaction
from skontio.banking.parsers import StatementError, is_mt940, parse_mt940, parse_statement
from skontio.banking.services import BankingError, import_statement

STATEMENT = (
    ":20:STARTUMS\r\n"
    ":25:10020030/1234567890\r\n"
    ":28C:00001/001\r\n"
    ":60F:C260301EUR1000,00\r\n"
    ":61:2603020302CR119,00NTRFNONREF//0001\r\n"
    ":86:166?00GUTSCHRIFT?20RE-2026-0001 Danke?21fuer den Auftrag?30BYLADEM1001?31DE02120300000000202051\r\n"
    "?32Kunde GmbH?33Abteilung Einkauf\r\n"
    ":61:260303D49,90NDDTNONREF\r\n"
    ":86:Hosting Abo Maerz\r\n"
    ":61:260304RC10,00NTRFNONREF\r\n"
    ":86:105?00RUECKBUCHUNG?20Storno Gutschrift?32Bank\r\n"
    ":62F:C260304EUR1059,10\r\n"
    "-\r\n"
)


def test_lines_signs_and_details():
    rows = parse_mt940(STATEMENT.encode("cp1252"))
    assert [(r.booked_on, r.amount) for r in rows] == [
        (dt.date(2026, 3, 2), Decimal("119.00")),
        (dt.date(2026, 3, 3), Decimal("-49.90")),
        (dt.date(2026, 3, 4), Decimal("-10.00")),  # RC = reversal of a credit
    ]
    first = rows[0]
    assert first.counterparty == "Kunde GmbH Abteilung Einkauf"
    assert first.iban == "DE02120300000000202051"
    assert first.purpose == "RE-2026-0001 Dankefuer den Auftrag"
    assert rows[1].purpose == "Hosting Abo Maerz" and rows[1].counterparty == ""
    assert {r.currency for r in rows} == {"EUR"}


def test_detected_and_imported_once(shop):
    data = STATEMENT.encode("cp1252")
    assert is_mt940(data) and parse_statement(data)[0] == "mt940"
    assert import_statement(shop.company, data) == (3, 0)
    assert import_statement(shop.company, data) == (0, 3)
    assert set(BankTransaction.objects.values_list("source", flat=True)) == {"mt940"}


def test_umlauts_in_windows_1252():
    data = (":20:X\n:60F:C260301EUR0,00\n:61:260302C5,00NTRF\n:86:166?20Gebühr Rückerstattung?32Müller\n").encode(
        "cp1252"
    )
    (row,) = parse_mt940(data)
    assert row.purpose == "Gebühr Rückerstattung" and row.counterparty == "Müller"


def test_swift_block_header_and_bogus_iban_are_handled():
    data = (
        b"{1:F01BANKDEFFXXXX0000000000}{2:I940BANKDEFFXXXXN}{4:\r\n:20:X\r\n:60F:C260301EUR0,00\r\n"
        b":61:260302C1,00NTRF\r\n:86:166?31NICHT-EINE-IBAN?32A\r\n-}"
    )
    (row,) = parse_mt940(data)
    assert row.iban == "" and row.counterparty == "A"


@pytest.mark.parametrize(
    "line",
    [":61:269902C1,00NTRF", ":61:260302X1,00NTRF", ":61:260302C9999999999999,00NTRF", ":61:abc"],
)
def test_broken_lines_raise_our_error(line):
    with pytest.raises(StatementError):
        parse_mt940(f":20:X\n:60F:C260301EUR0,00\n{line}\n".encode())


def test_not_an_mt940_file():
    with pytest.raises(StatementError):
        parse_mt940(b"Hallo Welt")


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.binary(max_size=3000))
def test_random_bytes_only_raise_our_own_error(data):
    try:
        rows = parse_mt940(b":20:X\n:61:" + data)
    except StatementError:
        return
    for row in rows:
        assert row.amount.is_finite() and len(row.purpose) <= 500 and len(row.counterparty) <= 200


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.text(max_size=400))
def test_random_purpose_text_never_crashes(text):
    data = f":20:X\n:60F:C260301EUR0,00\n:61:260302C1,00NTRF\n:86:166?20{text}\n".encode("utf-8", "replace")
    try:
        parse_mt940(data)
    except StatementError:
        pass


def test_import_refuses_garbage_cleanly(shop):
    with pytest.raises(BankingError):
        import_statement(shop.company, b":20:X\n:61:kaputt\n")
