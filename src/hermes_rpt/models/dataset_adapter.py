"""Converts `hermes_rpt.datasets.builder.DatasetRow`s into the plain numpy matrices scikit-learn
estimators consume (Phase 10). A missing feature (`None`, per Phase 8's "do not assume all
customers have every feature") becomes `NaN` — never a silently substituted zero here; each
baseline's own pipeline (`hermes_rpt.models.baselines`) decides how to handle `NaN` explicitly
(native support for gradient-boosted trees, imputation for logistic regression / MLP).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from hermes_rpt.datasets.builder import DatasetRow


def build_feature_matrix(
    rows: Sequence[DatasetRow], *, feature_names: tuple[str, ...]
) -> tuple[np.ndarray, np.ndarray]:
    """Returns `(X, y)` — `X` has one column per `feature_names` entry, in that fixed order, so
    a model trained on one dataset build is never accidentally fed a differently-ordered matrix
    at inference time (Phase 13's job, but the contract starts here)."""

    x = np.full((len(rows), len(feature_names)), np.nan, dtype=np.float64)
    y = np.empty(len(rows), dtype=np.int64)
    for row_index, row in enumerate(rows):
        y[row_index] = row.label
        for col_index, name in enumerate(feature_names):
            value = row.features.get(name)
            if value is not None:
                x[row_index, col_index] = float(value)
    return x, y
