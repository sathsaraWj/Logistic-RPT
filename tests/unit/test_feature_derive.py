"""Tests for the fixed derive-function registry (Phase 8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes_rpt.features.derive import UnknownDeriveFunctionError, apply_derive

_PREDICTION_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def test_hour_of_day() -> None:
    value = datetime(2026, 7, 27, 8, 30, tzinfo=UTC)
    assert apply_derive("hour_of_day", value, prediction_time=_PREDICTION_TIME) == 8


def test_hour_of_day_none_passthrough() -> None:
    assert apply_derive("hour_of_day", None, prediction_time=_PREDICTION_TIME) is None


def test_day_of_week_monday_is_zero() -> None:
    monday = datetime(2026, 7, 27, tzinfo=UTC)  # 2026-07-27 is a Monday
    assert apply_derive("day_of_week", monday, prediction_time=_PREDICTION_TIME) == 0


def test_age_years_at_prediction() -> None:
    acquired = datetime(2024, 7, 27, tzinfo=UTC)
    age = apply_derive("age_years_at_prediction", acquired, prediction_time=_PREDICTION_TIME)
    assert age == pytest.approx(2.0, abs=0.01)


def test_age_years_at_prediction_none_passthrough() -> None:
    assert apply_derive("age_years_at_prediction", None, prediction_time=_PREDICTION_TIME) is None


def test_loading_start_delay_minutes_when_already_departed() -> None:
    planned = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    actual = datetime(2026, 7, 27, 8, 15, tzinfo=UTC)
    delay = apply_derive(
        "loading_start_delay_minutes", actual, secondary=planned, prediction_time=_PREDICTION_TIME
    )
    assert delay == pytest.approx(15.0)


def test_loading_start_delay_minutes_is_zero_when_not_yet_departed() -> None:
    """The trip hasn't departed yet as of prediction_time — must be 0.0, not an error or a
    value computed from a None actual_departure_at."""

    delay = apply_derive(
        "loading_start_delay_minutes", None, secondary=None, prediction_time=_PREDICTION_TIME
    )
    assert delay == 0.0


def test_loading_start_delay_minutes_ignores_a_future_actual_departure() -> None:
    """Point-in-time safety: even if `actual_departure_at` is somehow populated with a value
    *after* prediction_time (shouldn't happen given the compiler's own filtering, but this is a
    second, independent guard), the derive function itself must still return 0.0, never a
    negative or leaked value."""

    planned = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    future_actual = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)  # after prediction_time
    delay = apply_derive(
        "loading_start_delay_minutes",
        future_actual,
        secondary=planned,
        prediction_time=_PREDICTION_TIME,
    )
    assert delay == 0.0


def test_unknown_derive_function_raises() -> None:
    with pytest.raises(UnknownDeriveFunctionError):
        apply_derive("not_a_real_function", None, prediction_time=_PREDICTION_TIME)
