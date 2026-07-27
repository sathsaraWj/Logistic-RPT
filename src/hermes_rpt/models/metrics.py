"""Evaluation metrics and threshold selection (Phase 10). "Because delivery delays may be
imbalanced, do not rely on accuracy alone" — accuracy is deliberately not computed here at all;
`EvaluationMetrics` covers the set the prompt requires instead (ROC-AUC, PR-AUC, precision,
recall, F1, Brier score, calibration, confusion matrix), every one of which stays meaningful
under class imbalance.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, ConfigDict
from sklearn.calibration import calibration_curve
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)


class CalibrationBin(BaseModel):
    model_config = ConfigDict(frozen=True)

    predicted_probability: float
    observed_frequency: float


class ConfusionMatrix(BaseModel):
    model_config = ConfigDict(frozen=True)

    true_negative: int
    false_positive: int
    false_negative: int
    true_positive: int


class EvaluationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    threshold: float
    roc_auc: float
    pr_auc: float
    precision: float
    recall: float
    f1: float
    brier_score: float
    confusion_matrix: ConfusionMatrix
    calibration: tuple[CalibrationBin, ...]


def select_threshold(
    y_true: np.ndarray, y_proba: np.ndarray, *, strategy: str, fixed_value: float
) -> float:
    if strategy == "fixed":
        return fixed_value
    if strategy != "max_f1":
        raise ValueError(f"Unknown threshold strategy: {strategy!r}")

    candidates = np.unique(y_proba)
    if candidates.size == 0:
        return fixed_value
    best_threshold = float(candidates[0])
    best_f1 = -1.0
    for candidate in candidates:
        predicted = (y_proba >= candidate).astype(int)
        score = f1_score(y_true, predicted, zero_division=0)
        if score > best_f1:
            best_f1 = float(score)
            best_threshold = float(candidate)
    return best_threshold


def compute_metrics(
    y_true: np.ndarray, y_proba: np.ndarray, *, threshold: float
) -> EvaluationMetrics:
    predicted = (y_proba >= threshold).astype(int)
    # roc_auc/pr_auc are undefined with only one class present (e.g. a tiny synthetic split) —
    # reported as 0.0 rather than raising, since a threshold/report still needs to be produced.
    has_both_classes = len(np.unique(y_true)) > 1
    roc_auc = float(roc_auc_score(y_true, y_proba)) if has_both_classes else 0.0
    pr_auc = float(average_precision_score(y_true, y_proba)) if has_both_classes else 0.0

    tn, fp, fn, tp = confusion_matrix(y_true, predicted, labels=[0, 1]).ravel()

    calibration_bins: tuple[CalibrationBin, ...] = ()
    if has_both_classes:
        observed, predicted_prob = calibration_curve(
            y_true, y_proba, n_bins=10, strategy="quantile"
        )
        calibration_bins = tuple(
            CalibrationBin(predicted_probability=float(p), observed_frequency=float(o))
            for p, o in zip(predicted_prob, observed, strict=True)
        )

    return EvaluationMetrics(
        threshold=threshold,
        roc_auc=roc_auc,
        pr_auc=pr_auc,
        precision=float(precision_score(y_true, predicted, zero_division=0)),
        recall=float(recall_score(y_true, predicted, zero_division=0)),
        f1=float(f1_score(y_true, predicted, zero_division=0)),
        brier_score=float(brier_score_loss(y_true, y_proba)),
        confusion_matrix=ConfusionMatrix(
            true_negative=int(tn),
            false_positive=int(fp),
            false_negative=int(fn),
            true_positive=int(tp),
        ),
        calibration=calibration_bins,
    )
