"""Query-cost guard (Phase 8): hard caps applied before any extraction query runs — "apply row
and time-window limits," "reject unbounded extraction." Independent of, and in addition to, the
connector-level statement timeout (`hermes_rpt.connectors.postgres`) and the mapping-time
allowlist checks (`hermes_rpt.connectors.query_guard`, `hermes_rpt.mappings.validation`).
"""

from __future__ import annotations

_MAX_HISTORICAL_WINDOW_DAYS = 365
_MAX_RELATED_ROW_LIMIT = 500
_MAX_DATASET_BUILD_ROW_LIMIT = 50_000


class QueryCostExceededError(Exception):
    pass


def enforce_window_limit(window_days: int | None) -> int | None:
    if window_days is not None and window_days > _MAX_HISTORICAL_WINDOW_DAYS:
        raise QueryCostExceededError(
            f"Historical window of {window_days} days exceeds the maximum of "
            f"{_MAX_HISTORICAL_WINDOW_DAYS} days"
        )
    return window_days


def related_row_limit() -> int:
    """A hard cap applied as a SQL `LIMIT` to every related-entity query — "reject unbounded
    extraction" holds even if a feature's window is large or a related table is huge."""

    return _MAX_RELATED_ROW_LIMIT


def dataset_build_row_limit() -> int:
    """A hard cap on how many target-entity rows a single dataset build enumerates
    (`hermes_rpt.features.compiler.fetch_rows_in_range`, `hermes_rpt.datasets.builder`) —
    same "reject unbounded extraction" principle applied to offline dataset building rather
    than a single online extraction."""

    return _MAX_DATASET_BUILD_ROW_LIMIT
