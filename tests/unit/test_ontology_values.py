"""Tests for the canonical value types (Phase 6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from hermes_rpt.ontology.values import CanonicalDatetime, Money


def test_money_preserves_currency_code() -> None:
    money = Money(amount="12.50", currency_code="usd")
    assert money.currency_code == "USD"  # normalised to upper case
    assert str(money.amount) == "12.50"


@pytest.mark.parametrize("bad_code", ["US", "USDD", "12D", ""])
def test_money_rejects_invalid_currency_codes(bad_code: str) -> None:
    with pytest.raises(ValidationError):
        Money(amount="1", currency_code=bad_code)


def test_canonical_datetime_requires_timezone_aware_utc() -> None:
    with pytest.raises(ValidationError):
        CanonicalDatetime(utc=datetime(2026, 1, 1, 12, 0, 0))  # naive — must be rejected


def test_canonical_datetime_accepts_timezone_aware_value_and_optional_original_zone() -> None:
    value = CanonicalDatetime(
        utc=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC), original_timezone="Africa/Nairobi"
    )
    assert value.utc.tzinfo is not None
    assert value.original_timezone == "Africa/Nairobi"


def test_canonical_datetime_original_timezone_is_optional() -> None:
    value = CanonicalDatetime(utc=datetime(2026, 1, 1, tzinfo=UTC))
    assert value.original_timezone is None
