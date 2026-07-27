"""Canonical value types and classification/type vocabularies for the Hermes ontology.

These exist so canonical fields carry their full semantics as a value, not just a bare number:
a distance is always kilometres (docs/IMPLEMENTATION_PLAN.md Phase 6 example), a monetary value
always carries its currency code, and a point in time is always UTC plus (optionally) the
timezone it was originally recorded in.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class LogicalType(StrEnum):
    """The base Python/Pydantic shape a field takes, independent of unit. See
    hermes_rpt.ontology.registry for how each maps to an actual Python annotation."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    ENUM = "enum"
    MONEY = "money"


class DataClassification(StrEnum):
    """Governance label for a field — not an access-control mechanism by itself, but the
    vocabulary `DataAccessPolicy` (hermes_rpt.tenants.models, Phase 2) and future feature
    extraction (Phase 8) can key off of."""

    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    SENSITIVE_PII = "sensitive_pii"


class RelationshipCardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class Money(BaseModel):
    """A monetary value that always carries its currency — "monetary values preserve their
    currency code" (Phase 6). Never converted to a single "canonical currency": unlike
    distance/volume/mass there is no unit-conversion layer for money, since exchange rates are
    time-varying market data, not a fixed physical constant."""

    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency_code: str

    @field_validator("currency_code")
    @classmethod
    def _validate_currency_code(cls, value: str) -> str:
        if len(value) != 3 or not value.isalpha():
            raise ValueError(f"currency_code must be a 3-letter ISO 4217 code, got {value!r}")
        return value.upper()


class CanonicalDatetime(BaseModel):
    """ "Datetimes are stored in UTC with original timezone metadata where needed" (Phase 6).
    `utc` is always timezone-aware and normalized to UTC; `original_timezone` (an IANA zone
    name, e.g. "Africa/Nairobi") is preserved separately when the source system recorded one,
    rather than losing it by collapsing straight to a bare UTC timestamp."""

    model_config = ConfigDict(frozen=True)

    utc: datetime
    original_timezone: str | None = None

    @field_validator("utc")
    @classmethod
    def _require_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("CanonicalDatetime.utc must be timezone-aware")
        return value
