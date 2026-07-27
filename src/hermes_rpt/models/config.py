"""Training configuration (Phase 10) — every field here is a training-run *input*, logged
verbatim as MLflow params (`hermes_rpt.models.training`) so a run is reproducible from its own
tracked metadata.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class BaselineModelType(StrEnum):
    LOGISTIC_REGRESSION = "logistic_regression"
    GRADIENT_BOOSTED_TREES = "gradient_boosted_trees"
    MLP = "mlp"


class ThresholdStrategy(StrEnum):
    FIXED = "fixed"  # uses threshold_value as-is
    MAX_F1 = "max_f1"  # picks the threshold maximizing F1 on the validation split


class TrainingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_type: BaselineModelType
    random_seed: int = 42
    threshold_strategy: ThresholdStrategy = ThresholdStrategy.MAX_F1
    threshold_value: float = 0.5  # only used when threshold_strategy is FIXED
    hyperparameters: dict[str, float | int | str | bool] = Field(default_factory=dict)
