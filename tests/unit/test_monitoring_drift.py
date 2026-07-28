"""Tests for `hermes_rpt.monitoring.drift` — feature/label distribution drift scoring."""

from __future__ import annotations

import pytest

from hermes_rpt.datasets.statistics import FeatureStatistic, LabelStatistics
from hermes_rpt.monitoring.drift import compute_feature_drift, compute_label_drift


def _stat(
    name: str, *, mean: float | None, std: float | None = 1.0, missing: int = 0
) -> FeatureStatistic:
    return FeatureStatistic(
        feature_name=name, count=100 - missing, missing_count=missing, mean=mean, std=std
    )


def test_identical_statistics_have_zero_drift() -> None:
    baseline = (_stat("x1", mean=10.0, std=2.0),)
    current = (_stat("x1", mean=10.0, std=2.0),)
    (result,) = compute_feature_drift(baseline, current)
    assert result.drift_score == 0.0
    assert result.significant is False


def test_a_large_mean_shift_is_significant() -> None:
    baseline = (_stat("x1", mean=10.0, std=2.0),)
    current = (_stat("x1", mean=20.0, std=2.0),)
    (result,) = compute_feature_drift(baseline, current)
    assert result.drift_score == 5.0  # (20-10)/2
    assert result.significant is True


def test_a_small_mean_shift_is_not_significant() -> None:
    baseline = (_stat("x1", mean=10.0, std=2.0),)
    current = (_stat("x1", mean=10.5, std=2.0),)
    (result,) = compute_feature_drift(baseline, current)
    assert result.significant is False


def test_missing_rate_delta_is_reported() -> None:
    baseline = (_stat("x1", mean=10.0, missing=0),)
    current = (_stat("x1", mean=10.0, missing=50),)
    (result,) = compute_feature_drift(baseline, current)
    assert result.missing_rate_delta == 0.5


def test_only_features_present_in_both_snapshots_are_compared() -> None:
    baseline = (_stat("x1", mean=1.0), _stat("x2", mean=2.0))
    current = (_stat("x1", mean=1.0), _stat("x3", mean=3.0))
    results = compute_feature_drift(baseline, current)
    assert {r.feature_name for r in results} == {"x1"}


def test_none_mean_produces_zero_drift_score_without_crashing() -> None:
    baseline = (_stat("x1", mean=None, std=None),)
    current = (_stat("x1", mean=None, std=None),)
    (result,) = compute_feature_drift(baseline, current)
    assert result.drift_score == 0.0


def test_label_drift_flags_a_large_positive_fraction_shift() -> None:
    baseline = LabelStatistics(count=100, positive_count=10, negative_count=90)
    current = LabelStatistics(count=100, positive_count=40, negative_count=60)
    result = compute_label_drift(baseline, current)
    assert result.drift_score == pytest.approx(0.3)  # 0.4 - 0.1
    assert result.significant is True


def test_label_drift_does_not_flag_a_small_shift() -> None:
    baseline = LabelStatistics(count=100, positive_count=10, negative_count=90)
    current = LabelStatistics(count=100, positive_count=12, negative_count=88)
    result = compute_label_drift(baseline, current)
    assert result.significant is False
