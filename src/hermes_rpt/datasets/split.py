"""Temporal train/validation/test splitting (Phase 9) — "use temporal splits rather than random
splits for delay prediction" and "avoid placing future events into earlier training rows."
Rows are sorted by `prediction_time` and cut by fraction; there is no shuffling, so every row in
`train` has a `prediction_time` no later than any row in `val`, which in turn precedes `test`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from hermes_rpt.datasets.definition import TemporalSplitStrategy


class DatasetSplits[RowT](BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    train: tuple[RowT, ...]
    validation: tuple[RowT, ...]
    test: tuple[RowT, ...]


def split_temporally[RowT](
    sorted_rows: list[RowT], *, strategy: TemporalSplitStrategy
) -> DatasetSplits[RowT]:
    """`sorted_rows` must already be sorted ascending by `prediction_time` — the caller
    (`hermes_rpt.datasets.builder`) fetches rows via `fetch_rows_in_range`, which already orders
    by the time column, so no second sort happens here."""

    n = len(sorted_rows)
    train_end = int(n * strategy.train_fraction)
    validation_end = train_end + int(n * strategy.validation_fraction)
    return DatasetSplits(
        train=tuple(sorted_rows[:train_end]),
        validation=tuple(sorted_rows[train_end:validation_end]),
        test=tuple(sorted_rows[validation_end:]),
    )
