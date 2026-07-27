"""Pure Python derive functions for `FeatureKind.DERIVED_FROM_TARGET` (and the post-processing
step of `RELATED_LATEST_VALUE` when a `derive` is set) — Phase 8's fixed, safe set, never
arbitrary code from a feature spec.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any


class UnknownDeriveFunctionError(Exception):
    pass


def hour_of_day(value: datetime, **_: Any) -> int | None:
    return value.hour if value is not None else None


def day_of_week(value: datetime, **_: Any) -> int | None:
    return value.weekday() if value is not None else None


def age_years_at_prediction(
    value: datetime, *, prediction_time: datetime, **_: Any
) -> float | None:
    if value is None:
        return None
    return (prediction_time - value).days / 365.25


def loading_start_delay_minutes(
    value: datetime | None, *, secondary: datetime | None, prediction_time: datetime, **_: Any
) -> float:
    """`value` is `actual_departure_at`, `secondary` is `planned_departure_at`. Only computed
    when the trip has *already* departed strictly before `prediction_time` — otherwise 0.0,
    never a value derived from a departure that (as of prediction_time) hasn't happened yet."""

    if value is None or secondary is None or value >= prediction_time:
        return 0.0
    return (value - secondary).total_seconds() / 60


_REGISTRY: dict[str, Callable[..., Any]] = {
    "hour_of_day": hour_of_day,
    "day_of_week": day_of_week,
    "age_years_at_prediction": age_years_at_prediction,
    "loading_start_delay_minutes": loading_start_delay_minutes,
}


def apply_derive(
    name: str,
    value: Any,
    *,
    secondary: Any = None,
    prediction_time: datetime,
) -> Any:
    try:
        fn = _REGISTRY[name]
    except KeyError as exc:
        raise UnknownDeriveFunctionError(f"Unknown derive function: {name!r}") from exc
    return fn(value, secondary=secondary, prediction_time=prediction_time)
