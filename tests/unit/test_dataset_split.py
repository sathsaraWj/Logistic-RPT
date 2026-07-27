"""Tests for temporal train/validation/test splitting (Phase 9)."""

from __future__ import annotations

from hermes_rpt.datasets.definition import TemporalSplitStrategy
from hermes_rpt.datasets.split import split_temporally


def test_split_respects_fractions() -> None:
    rows = list(range(100))
    splits = split_temporally(
        rows, strategy=TemporalSplitStrategy(train_fraction=0.7, validation_fraction=0.15)
    )
    assert len(splits.train) == 70
    assert len(splits.validation) == 15
    assert len(splits.test) == 15


def test_split_preserves_order_no_shuffling() -> None:
    rows = list(range(10))
    splits = split_temporally(
        rows, strategy=TemporalSplitStrategy(train_fraction=0.6, validation_fraction=0.2)
    )
    assert splits.train == (0, 1, 2, 3, 4, 5)
    assert splits.validation == (6, 7)
    assert splits.test == (8, 9)


def test_split_every_train_row_precedes_every_validation_and_test_row() -> None:
    # sorted_rows simulates rows already ordered by ascending prediction_time.
    rows = list(range(50))
    splits = split_temporally(rows, strategy=TemporalSplitStrategy())
    assert max(splits.train) < min(splits.validation)
    assert max(splits.validation) < min(splits.test)


def test_split_handles_small_datasets() -> None:
    rows = [1, 2, 3]
    splits = split_temporally(rows, strategy=TemporalSplitStrategy())
    assert len(splits.train) + len(splits.validation) + len(splits.test) == 3
