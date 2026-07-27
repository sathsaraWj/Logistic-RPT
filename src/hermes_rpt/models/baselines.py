"""The three baseline models (Phase 10): logistic regression, gradient-boosted trees, and a
small MLP — all scikit-learn, deliberately not torch, so the baseline tier stays independent of
Phase 11's relational-transformer stack and "do not require every optional library if it
complicates portability" holds for anything short of the transformer itself.
"""

from __future__ import annotations

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from hermes_rpt.models.config import BaselineModelType, TrainingConfig


class LogisticRegressionBaseline:
    """Median-imputes and standard-scales first — plain `LogisticRegression` assumes neither
    missing values nor comparable feature scales, unlike the tree-based baseline."""

    name = "logistic_regression"

    def __init__(self, config: TrainingConfig) -> None:
        self._pipeline = Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                (
                    "classifier",
                    LogisticRegression(
                        random_state=config.random_seed,
                        max_iter=1000,
                        **_typed_hyperparameters(config, {"C", "penalty", "solver"}),
                    ),
                ),
            ]
        )

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        self._pipeline.fit(X, y)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(self._pipeline.predict_proba(X)[:, 1])

    def feature_importance(self, feature_names: tuple[str, ...]) -> dict[str, float] | None:
        classifier = self._pipeline.named_steps["classifier"]
        coefficients = classifier.coef_[0]
        return dict(zip(feature_names, (float(c) for c in coefficients), strict=True))


class GradientBoostedTreesBaseline:
    """`HistGradientBoostingClassifier` — chosen over a plain `GradientBoostingClassifier`
    specifically because it handles `NaN` natively. That matters here: Phase 8/9's own design
    means a real fraction of feature values are legitimately missing per tenant ("do not assume
    all customers have every feature"), and imputing them away would throw out exactly the
    "this tenant doesn't have this data" signal a tree split can otherwise use directly."""

    name = "gradient_boosted_trees"

    def __init__(self, config: TrainingConfig) -> None:
        allowed = {"max_iter", "max_depth", "learning_rate", "l2_regularization"}
        self._model = HistGradientBoostingClassifier(
            random_state=config.random_seed, **_typed_hyperparameters(config, allowed)
        )

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        self._model.fit(X, y)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(self._model.predict_proba(X)[:, 1])

    def feature_importance(self, feature_names: tuple[str, ...]) -> dict[str, float] | None:
        # HistGradientBoostingClassifier has no built-in feature_importances_ (unlike
        # RandomForest/plain GradientBoosting) — a real "not supported by this model kind" case,
        # not a placeholder. Permutation importance would need held-out data this interface
        # doesn't carry; a future phase can add it if this baseline becomes the chosen one.
        return None


class MLPBaseline:
    """A small MLP — two modest hidden layers, not a deep network. Explicitly the "small
    multilayer perceptron" baseline the phase asks for, kept separate from Phase 11's
    torch-based relational transformer."""

    name = "mlp"

    def __init__(self, config: TrainingConfig) -> None:
        hidden_layer_sizes = config.hyperparameters.get("hidden_layer_sizes", "32,16")
        sizes = tuple(int(s) for s in str(hidden_layer_sizes).split(","))
        self._pipeline = Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                (
                    "classifier",
                    MLPClassifier(
                        hidden_layer_sizes=sizes,
                        random_state=config.random_seed,
                        max_iter=500,
                        early_stopping=True,
                    ),
                ),
            ]
        )

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        self._pipeline.fit(X, y)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(self._pipeline.predict_proba(X)[:, 1])

    def feature_importance(self, feature_names: tuple[str, ...]) -> dict[str, float] | None:
        return None  # an MLP's weights aren't a direct per-feature importance signal


def _typed_hyperparameters(
    config: TrainingConfig, allowed: set[str]
) -> dict[str, float | int | str | bool]:
    """Only forwards hyperparameters the target estimator actually accepts — `hyperparameters`
    is a free-form dict from `TrainingConfig`, and passing an unrecognized kwarg straight to a
    scikit-learn constructor raises `TypeError`, not a clean validation error."""

    return {k: v for k, v in config.hyperparameters.items() if k in allowed}


Baseline = LogisticRegressionBaseline | GradientBoostedTreesBaseline | MLPBaseline


def build_baseline(config: TrainingConfig) -> Baseline:
    if config.model_type == BaselineModelType.LOGISTIC_REGRESSION:
        return LogisticRegressionBaseline(config)
    if config.model_type == BaselineModelType.GRADIENT_BOOSTED_TREES:
        return GradientBoostedTreesBaseline(config)
    if config.model_type == BaselineModelType.MLP:
        return MLPBaseline(config)
    raise ValueError(f"Unknown baseline model type: {config.model_type}")  # pragma: no cover
