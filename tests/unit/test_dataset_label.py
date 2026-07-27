"""Tests for the fixed label-function registry (Phase 9)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes_rpt.datasets.label import UnknownLabelFunctionError, compute_label, delivery_delay_label

_DEPARTURE = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
_PLANNED_ARRIVAL = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def _row(**overrides: object) -> dict[str, object]:
    base = {
        "status": "completed",
        "planned_arrival_at": _PLANNED_ARRIVAL,
        "actual_arrival_at": _PLANNED_ARRIVAL,
    }
    base.update(overrides)
    return base


def test_on_time_trip_is_negative_class() -> None:
    assert delivery_delay_label(_row(actual_arrival_at=_PLANNED_ARRIVAL)) == 0


def test_trip_delayed_beyond_threshold_is_positive_class() -> None:
    late = _PLANNED_ARRIVAL.replace(hour=11)  # 60 minutes late
    assert delivery_delay_label(_row(actual_arrival_at=late)) == 1


def test_trip_delayed_under_threshold_is_negative_class() -> None:
    slightly_late = _PLANNED_ARRIVAL.replace(minute=20)  # 20 minutes late, under the 30min default
    assert delivery_delay_label(_row(actual_arrival_at=slightly_late)) == 0


def test_cancelled_trip_is_positive_class() -> None:
    assert delivery_delay_label(_row(status="cancelled")) == 1


@pytest.mark.parametrize("status", ["planned", "in_progress", None])
def test_trip_without_a_known_outcome_has_no_label(status: str | None) -> None:
    assert delivery_delay_label(_row(status=status)) is None


def test_unrecognised_status_has_no_label() -> None:
    assert delivery_delay_label(_row(status="delayed_indefinitely")) is None


def test_completed_trip_missing_a_timestamp_has_no_label() -> None:
    assert delivery_delay_label(_row(actual_arrival_at=None)) is None


def test_custom_delay_threshold() -> None:
    late_by_45 = _PLANNED_ARRIVAL.replace(hour=10, minute=45)
    assert delivery_delay_label(_row(actual_arrival_at=late_by_45), delay_threshold_minutes=60) == 0
    assert delivery_delay_label(_row(actual_arrival_at=late_by_45), delay_threshold_minutes=30) == 1


def test_compute_label_dispatches_by_name() -> None:
    assert compute_label("delivery_delay_label", _row()) == 0


def test_compute_label_unknown_name_raises() -> None:
    with pytest.raises(UnknownLabelFunctionError):
        compute_label("not_a_real_label_function", _row())
