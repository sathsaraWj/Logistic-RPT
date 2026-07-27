"""Fixed label functions for prediction tasks (Phase 9) — the label-side counterpart to
`hermes_rpt.features.derive`'s fixed derive registry: a label is computed by one of a small,
named set of Python functions, never generated or evaluated from arbitrary code.

A label can only be computed once the outcome it describes has actually happened — "avoid
placing future events into earlier training rows" applies to labels just as much as to
features. Every function here returns `None` when the outcome isn't known yet (e.g. a trip
that's still `in_progress`), which the caller (`hermes_rpt.datasets.builder`) must treat as "not
labelable yet," not as a missing-value placeholder to train on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

_DEFAULT_DELAY_THRESHOLD_MINUTES = 30


class UnknownLabelFunctionError(Exception):
    pass


def delivery_delay_label(
    row: dict[str, Any], *, delay_threshold_minutes: int = _DEFAULT_DELAY_THRESHOLD_MINUTES
) -> int | None:
    """1 if the trip was delivered late (or never delivered at all), 0 if it completed on
    time, `None` if the outcome isn't known yet.

    `row` must carry `status`, `planned_arrival_at`, and `actual_arrival_at` — the label-relevant
    fields `hermes_rpt.datasets.builder` fetches separately from (and never passes into) feature
    extraction, so the label itself can never leak into the feature set.
    """

    status = row.get("status")
    if status in (None, "planned", "in_progress"):
        return None  # outcome not known yet — excluded from the dataset, not labeled 0
    if status == "cancelled":
        return 1  # never delivered — treated as the positive (delay/failure) class
    if status != "completed":
        return None  # unrecognised status — hermes_rpt.datasets.quality flags this separately

    planned_arrival_at = row.get("planned_arrival_at")
    actual_arrival_at = row.get("actual_arrival_at")
    if not isinstance(planned_arrival_at, datetime) or not isinstance(actual_arrival_at, datetime):
        return None  # completed but missing a timestamp needed to judge lateness — not labelable

    delay_minutes = (actual_arrival_at - planned_arrival_at).total_seconds() / 60
    return 1 if delay_minutes > delay_threshold_minutes else 0


_REGISTRY = {"delivery_delay_label": delivery_delay_label}


def compute_label(name: str, row: dict[str, Any], **kwargs: Any) -> int | None:
    try:
        fn = _REGISTRY[name]
    except KeyError as exc:
        raise UnknownLabelFunctionError(f"Unknown label function: {name!r}") from exc
    return fn(row, **kwargs)
