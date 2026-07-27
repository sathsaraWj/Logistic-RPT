"""Pure Python derive functions for `FeatureKind.DERIVED_FROM_TARGET` (and the post-processing
step of `RELATED_LATEST_VALUE` when a `derive` is set) — Phase 8's fixed, safe set, never
arbitrary code from a feature spec.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


class UnknownDeriveFunctionError(Exception):
    pass


def _coerce_datetime(value: Any) -> datetime | None:
    """Postgres/asyncpg always returns a real, timezone-aware `datetime` for a timestamp
    column; this exists only for portability with drivers (e.g. sqlite3, used in tests) that
    round-trip a TEXT-affinity column as a naive ISO string instead — the platform's datetimes
    are always UTC (hermes_rpt.ontology.values.CanonicalDatetime), so a naive value here is
    assumed to already be UTC. Mirrors the identical fallback in
    hermes_rpt.features.compiler.compile_and_run_related_feature."""

    if value is None:
        return None
    parsed: datetime = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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
    return fn(
        _coerce_datetime(value),
        secondary=_coerce_datetime(secondary),
        prediction_time=prediction_time,
    )
