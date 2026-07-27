"""Common baseline model interface (Phase 10) — every baseline
(`hermes_rpt.models.baselines`) implements this, so `hermes_rpt.models.training` never branches
on which algorithm it's training.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class BaselineModel(Protocol):
    """`fit`/`predict_proba` mirror scikit-learn's estimator interface deliberately — every
    concrete baseline *is* (or wraps) a scikit-learn estimator, so nothing here reinvents that
    shape, just names it as a contract `hermes_rpt.models.training` can rely on regardless of
    which specific algorithm is behind it."""

    name: str

    def fit(self, X: np.ndarray, y: np.ndarray) -> None: ...

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Returns P(label=1) for each row — a 1-D array, not sklearn's 2-column form."""
        ...

    def feature_importance(self, feature_names: tuple[str, ...]) -> dict[str, float] | None:
        """None for a model kind with no natural importance signal (Phase 10: "feature
        importance or explainability where supported" — not every baseline supports it)."""
        ...
