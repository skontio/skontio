"""GiroCode / EPC QR code (EPC069-12, version 002) for SEPA credit transfers."""

from __future__ import annotations

import base64
import io
from decimal import Decimal

import segno

MAX_AMOUNT = Decimal("999999999.99")
MAX_PAYLOAD_BYTES = 331


def epc_payload(*, name: str, iban: str, amount: Decimal, reference: str, bic: str = "") -> str:
    if not (Decimal("0.01") <= amount <= MAX_AMOUNT):
        raise ValueError("Der GiroCode-Betrag muss zwischen 0,01 € und 999.999.999,99 € liegen")
    iban = iban.replace(" ", "").upper()
    lines = [
        "BCD",
        "002",
        "1",  # UTF-8
        "SCT",
        bic.replace(" ", "").upper(),
        name.strip()[:70],
        iban,
        f"EUR{amount:.2f}",
        "",  # purpose code
        "",  # structured creditor reference
        reference.strip()[:140],  # unstructured remittance information
    ]
    payload = "\n".join(lines)
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("GiroCode-Inhalt ist zu lang")
    return payload


def girocode_svg_data_uri(*, name: str, iban: str, amount: Decimal, reference: str, bic: str = "") -> str:
    payload = epc_payload(name=name, iban=iban, amount=amount, reference=reference, bic=bic)
    qr = segno.make(payload, error="m", encoding="utf-8", micro=False)  # EPC requires error level M
    buffer = io.BytesIO()
    qr.save(buffer, kind="svg", scale=4, border=2, xmldecl=False)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
