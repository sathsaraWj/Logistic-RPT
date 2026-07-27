"""Tests for the profiling classification rules (Phase 5) — the pure, DB-free logic. Actual
sampling against a real database is covered by tests/integration."""

from __future__ import annotations

import pytest

from hermes_rpt.schemas.profiling import (
    _assert_safe_identifier,
    is_credential_column,
    is_likely_personal_field,
)


@pytest.mark.parametrize(
    "column_name",
    ["password", "api_key", "secret_token", "access_token", "private_key", "db_credential"],
)
def test_credential_columns_are_detected(column_name: str) -> None:
    assert is_credential_column(column_name, extra_denylist=[])


def test_ordinary_columns_are_not_flagged_as_credentials() -> None:
    assert not is_credential_column("odometer_km", extra_denylist=[])


def test_custom_denylist_pattern_is_honoured() -> None:
    assert is_credential_column("internal_ref_code", extra_denylist=["internal_ref"])
    assert not is_credential_column("internal_ref_code", extra_denylist=[])


@pytest.mark.parametrize(
    "column_name",
    ["email", "e_mail", "phone_number", "home_address", "first_name", "date_of_birth", "ssn"],
)
def test_likely_personal_fields_are_detected(column_name: str) -> None:
    assert is_likely_personal_field(column_name)


def test_ordinary_columns_are_not_flagged_as_personal() -> None:
    assert not is_likely_personal_field("vehicle_id")


def test_assert_safe_identifier_rejects_embedded_quote() -> None:
    with pytest.raises(ValueError, match="Unsafe identifier"):
        _assert_safe_identifier('fleet_vehicle"; DROP TABLE users; --')


def test_assert_safe_identifier_accepts_ordinary_names() -> None:
    _assert_safe_identifier("fleet_vehicle")  # must not raise
