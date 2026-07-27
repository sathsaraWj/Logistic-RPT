"""Unit tests for the baseline models, metrics, dataset adapter, and comparison report
(Phase 10) — pure logic, no database or MLflow involved.
"""

from __future__ import annotations

import numpy as np
import pytest

from hermes_rpt.datasets.builder import DatasetRow
from hermes_rpt.models.baselines import (
    GradientBoostedTreesBaseline,
    LogisticRegressionBaseline,
    MLPBaseline,
    build_baseline,
)
from hermes_rpt.models.comparison import build_comparison_report
from hermes_rpt.models.config import BaselineModelType, ThresholdStrategy, TrainingConfig
from hermes_rpt.models.dataset_adapter import build_feature_matrix
from hermes_rpt.models.metrics import EvaluationMetrics, compute_metrics, select_threshold
from hermes_rpt.models.training import TrainingResult

_FEATURE_NAMES = ("a", "b", "c")


def _synthetic_xy(n: int = 200, *, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, len(_FEATURE_NAMES)))
    # y correlates with column 0 so the models have something real to learn.
    y = (x[:, 0] + rng.normal(scale=0.5, size=n) > 0).astype(int)
    return x, y


@pytest.mark.parametrize(
    "model_type",
    [
        BaselineModelType.LOGISTIC_REGRESSION,
        BaselineModelType.GRADIENT_BOOSTED_TREES,
        BaselineModelType.MLP,
    ],
)
def test_baseline_fits_and_predicts_probabilities_in_range(model_type: BaselineModelType) -> None:
    x, y = _synthetic_xy()
    model = build_baseline(TrainingConfig(model_type=model_type))
    model.fit(x, y)
    proba = model.predict_proba(x)
    assert proba.shape == (len(y),)
    assert np.all((proba >= 0) & (proba <= 1))


@pytest.mark.parametrize(
    "model_type",
    [
        BaselineModelType.LOGISTIC_REGRESSION,
        BaselineModelType.GRADIENT_BOOSTED_TREES,
        BaselineModelType.MLP,
    ],
)
def test_baseline_is_reproducible_given_the_same_seed(model_type: BaselineModelType) -> None:
    x, y = _synthetic_xy()
    config = TrainingConfig(model_type=model_type, random_seed=7)

    model_a = build_baseline(config)
    model_a.fit(x, y)
    model_b = build_baseline(config)
    model_b.fit(x, y)

    np.testing.assert_allclose(model_a.predict_proba(x), model_b.predict_proba(x))


def test_gradient_boosted_trees_handles_missing_values_natively() -> None:
    x, y = _synthetic_xy()
    x_with_gaps = x.copy()
    x_with_gaps[::5, 0] = np.nan  # every 5th row missing its first feature
    model = GradientBoostedTreesBaseline(
        TrainingConfig(model_type=BaselineModelType.GRADIENT_BOOSTED_TREES)
    )
    model.fit(x_with_gaps, y)  # must not raise
    proba = model.predict_proba(x_with_gaps)
    assert not np.any(np.isnan(proba))


@pytest.mark.parametrize("cls", [LogisticRegressionBaseline, MLPBaseline])
def test_imputing_baselines_handle_missing_values_via_imputation(cls: type) -> None:
    x, y = _synthetic_xy()
    x_with_gaps = x.copy()
    x_with_gaps[::5, 0] = np.nan
    config = TrainingConfig(
        model_type=BaselineModelType.LOGISTIC_REGRESSION
        if cls is LogisticRegressionBaseline
        else BaselineModelType.MLP
    )
    model = cls(config)
    model.fit(x_with_gaps, y)  # must not raise — the imputer runs before the classifier
    proba = model.predict_proba(x_with_gaps)
    assert not np.any(np.isnan(proba))


def test_logistic_regression_feature_importance_is_keyed_by_feature_name() -> None:
    x, y = _synthetic_xy()
    model = LogisticRegressionBaseline(
        TrainingConfig(model_type=BaselineModelType.LOGISTIC_REGRESSION)
    )
    model.fit(x, y)
    importance = model.feature_importance(_FEATURE_NAMES)
    assert importance is not None
    assert set(importance) == set(_FEATURE_NAMES)


def test_gradient_boosted_trees_feature_importance_is_not_supported() -> None:
    x, y = _synthetic_xy()
    model = GradientBoostedTreesBaseline(
        TrainingConfig(model_type=BaselineModelType.GRADIENT_BOOSTED_TREES)
    )
    model.fit(x, y)
    assert model.feature_importance(_FEATURE_NAMES) is None


def test_predict_proba_rejects_a_mismatched_feature_count() -> None:
    """Input-schema validation: a matrix with the wrong number of columns must fail loudly, not
    silently produce a nonsensical prediction — this is scikit-learn's own shape check, exercised
    here as a guarantee the platform relies on."""

    x, y = _synthetic_xy()
    model = build_baseline(TrainingConfig(model_type=BaselineModelType.LOGISTIC_REGRESSION))
    model.fit(x, y)
    wrong_shape = x[:, :-1]  # one fewer column than trained on
    with pytest.raises(ValueError):
        model.predict_proba(wrong_shape)


def test_build_feature_matrix_uses_nan_for_missing_features() -> None:
    rows = [
        DatasetRow(
            business_reference="r1", prediction_time=_now(), features={"a": 1.0, "b": None}, label=0
        ),
        DatasetRow(
            business_reference="r2", prediction_time=_now(), features={"a": 2.0, "b": 3.0}, label=1
        ),
    ]
    x, y = build_feature_matrix(rows, feature_names=("a", "b"))
    assert x.shape == (2, 2)
    assert np.isnan(x[0, 1])
    assert x[1, 1] == 3.0
    np.testing.assert_array_equal(y, [0, 1])


def test_build_feature_matrix_preserves_column_order() -> None:
    rows = [
        DatasetRow(
            business_reference="r1", prediction_time=_now(), features={"b": 2.0, "a": 1.0}, label=0
        )
    ]
    x, _y = build_feature_matrix(rows, feature_names=("a", "b"))
    assert x[0, 0] == 1.0
    assert x[0, 1] == 2.0


def _now():  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    return datetime(2026, 1, 1, tzinfo=UTC)


def test_select_threshold_fixed_returns_the_fixed_value() -> None:
    y = np.array([0, 1, 0, 1])
    proba = np.array([0.1, 0.9, 0.2, 0.8])
    assert select_threshold(y, proba, strategy="fixed", fixed_value=0.42) == 0.42


def test_select_threshold_max_f1_finds_a_separating_threshold() -> None:
    y = np.array([0, 0, 0, 1, 1, 1])
    proba = np.array([0.1, 0.2, 0.3, 0.7, 0.8, 0.9])
    threshold = select_threshold(y, proba, strategy="max_f1", fixed_value=0.5)
    predicted = (proba >= threshold).astype(int)
    np.testing.assert_array_equal(predicted, y)  # a perfectly separable case should get F1=1.0


def test_select_threshold_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError):
        select_threshold(np.array([0, 1]), np.array([0.1, 0.9]), strategy="bogus", fixed_value=0.5)


def test_compute_metrics_returns_a_full_confusion_matrix() -> None:
    y = np.array([0, 0, 1, 1])
    proba = np.array([0.1, 0.6, 0.4, 0.9])
    metrics = compute_metrics(y, proba, threshold=0.5)
    cm = metrics.confusion_matrix
    assert cm.true_negative + cm.false_positive + cm.false_negative + cm.true_positive == 4


def test_compute_metrics_handles_a_single_class_split_without_raising() -> None:
    y = np.array([0, 0, 0])
    proba = np.array([0.1, 0.2, 0.3])
    metrics = compute_metrics(y, proba, threshold=0.5)
    assert metrics.roc_auc == 0.0
    assert metrics.pr_auc == 0.0


def test_threshold_strategy_enum_round_trips_through_config() -> None:
    config = TrainingConfig(
        model_type=BaselineModelType.LOGISTIC_REGRESSION, threshold_strategy=ThresholdStrategy.FIXED
    )
    assert config.threshold_strategy == ThresholdStrategy.FIXED


def _fake_result(model_type: str, pr_auc: float) -> TrainingResult:
    metrics = EvaluationMetrics(
        threshold=0.5,
        roc_auc=0.7,
        pr_auc=pr_auc,
        precision=0.5,
        recall=0.5,
        f1=0.5,
        brier_score=0.2,
        confusion_matrix={
            "true_negative": 1,
            "false_positive": 1,
            "false_negative": 1,
            "true_positive": 1,
        },
        calibration=(),
    )
    return TrainingResult(
        model_type=model_type,
        metrics=metrics,
        feature_importance=None,
        mlflow_run_id="fake-run",
        model_version_id="00000000-0000-0000-0000-000000000000",
        artifact_checksum="a" * 64,
    )


def test_comparison_report_selects_the_highest_pr_auc_model() -> None:
    results = [
        _fake_result("logistic_regression", 0.3),
        _fake_result("gradient_boosted_trees", 0.6),
    ]
    report = build_comparison_report(results)
    assert report.selected_model_type == "gradient_boosted_trees"
    assert "gradient_boosted_trees" in report.as_markdown()
    assert "**(selected)**" in report.as_markdown()


def test_comparison_report_rejects_an_empty_result_list() -> None:
    with pytest.raises(ValueError):
        build_comparison_report([])
