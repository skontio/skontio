"""Shared input types with hard limits (plan §7.3: Feldlimits). Models reject unknown fields."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
SKU = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]
Query = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
UnitCode = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z0-9]{1,3}$"),
    Field(description="UN/ECE-Einheitencode, z. B. HUR (Stunde), C62 (Stück), DAY (Tag), MON (Monat), LS (pauschal)"),
]
Money = Annotated[Decimal, Field(ge=0, le=Decimal("10000000"), max_digits=14, decimal_places=4)]
Quantity = Annotated[Decimal, Field(ge=0, le=Decimal("1000000"), max_digits=14, decimal_places=4)]
Percent = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
CustomerNumber = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
DocumentNumber = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
IdempotencyKey = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9._:-]{8,100}$"),
    Field(
        description="Pflicht: eindeutiger Schlüssel je Vorgang (z. B. UUID). Derselbe Schlüssel liefert den "
        "bereits angelegten Entwurf zurück statt einen zweiten anzulegen."
    ),
]
Limit = Annotated[int, Field(ge=1, le=50)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
