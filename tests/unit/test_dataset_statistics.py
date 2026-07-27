"""Tests for feature/label statistics (Phase 9)."""

from __future__ import annotations

import pytest

from hermes_rpt.datasets.statistics import compute_feature_statistics, compute_label_statistics


def test_compute_feature_statistics_computes_mean_std_min_max() -> None:
    rows = [{"x": 1.0}, {"x": 2.0}, {"x": 3.0}]
    (stat,) = compute_feature_statistics(rows, feature_names=("x",))
    assert stat.count == 3
    assert stat.missing_count == 0
    assert stat.mean == pytest.approx(2.0)
    assert stat.min == 1.0
    assert stat.max == 3.0


def test_compute_feature_statistics_counts_missing_values() -> None:
    rows = [{"x": 1.0}, {"x": None}, {}]
    (stat,) = compute_feature_statistics(rows, feature_names=("x",))
    assert stat.count == 1
    assert stat.missing_count == 2


def test_compute_feature_statistics_handles_all_missing() -> None:
    rows = [{"x": None}, {"x": None}]
    (stat,) = compute_feature_statistics(rows, feature_names=("x",))
    assert stat.count == 0
    assert stat.missing_count == 2
    assert stat.mean is None


def test_compute_feature_statistics_coerces_booleans() -> None:
    rows = [{"flag": True}, {"flag": False}]
    (stat,) = compute_feature_statistics(rows, feature_names=("flag",))
    assert stat.mean == pytest.approx(0.5)


def test_compute_label_statistics_counts_classes() -> None:
    stats = compute_label_statistics([1, 0, 1, 1, 0])
    assert stats.count == 5
    assert stats.positive_count == 3
    assert stats.negative_count == 2
    assert stats.positive_fraction == pytest.approx(0.6)


def test_compute_label_statistics_empty() -> None:
    stats = compute_label_statistics([])
    assert stats.count == 0
    assert stats.positive_fraction == 0.0
