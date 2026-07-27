"""What a dataset build is asked to produce (Phase 9) — the input to
`hermes_rpt.datasets.builder.DatasetBuildService`, analogous to how `FeatureContract` is the
input to `FeatureExtractionService`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LabelDefinition(BaseModel):
    """Which fixed label function (`hermes_rpt.datasets.label`) to run and its keyword
    arguments. `source_fields` are every raw target-row field the build needs *outside* of
    `FeatureContract` — both what `function_name` itself consumes and what
    `hermes_rpt.datasets.quality`'s raw-row checks validate (e.g. `planned_distance_km` for
    `check_negative_numeric`, even though no label function reads it). Fetched directly by the
    dataset builder, deliberately never through `FeatureContract`, so a label-determining field
    (e.g. `actual_arrival_at`) can never accidentally also be extracted as a feature and leak the
    label into the training input."""

    model_config = ConfigDict(frozen=True)

    function_name: str
    source_fields: tuple[str, ...] = Field(min_length=1)
    time_field: str  # the field fetch_rows_in_range enumerates candidate rows by
    kwargs: dict[str, int | float | str] = Field(default_factory=dict)


class TemporalSplitStrategy(BaseModel):
    """Rows are sorted by `prediction_time` and cut by fraction, not shuffled — "use temporal
    splits rather than random splits for delay prediction." Every row in `train` has an earlier
    (or equal, at the boundary) `prediction_time` than every row in `val`, which in turn precedes
    every row in `test` — see `hermes_rpt.datasets.split`."""

    model_config = ConfigDict(frozen=True)

    train_fraction: float = 0.7
    validation_fraction: float = 0.15
    # test_fraction is implicit: 1 - train_fraction - validation_fraction

    @model_validator(mode="after")
    def _fractions_are_valid(self) -> TemporalSplitStrategy:
        if not (0 < self.train_fraction < 1) or not (0 < self.validation_fraction < 1):
            raise ValueError("train_fraction and validation_fraction must each be in (0, 1)")
        if self.train_fraction + self.validation_fraction >= 1:
            raise ValueError(
                "train_fraction + validation_fraction must leave room for a test split"
            )
        return self


class DatasetDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_key: str
    task_key: str
    tenant_id: uuid.UUID | None  # None only for an explicitly approved shared research dataset
    is_shared_research_dataset: bool = False
    time_range_start: datetime
    time_range_end: datetime
    label: LabelDefinition
    split_strategy: TemporalSplitStrategy = TemporalSplitStrategy()

    @model_validator(mode="after")
    def _tenant_or_shared(self) -> DatasetDefinition:
        if self.tenant_id is None and not self.is_shared_research_dataset:
            raise ValueError(
                "tenant_id is required unless is_shared_research_dataset is explicitly set — "
                "'every dataset belongs to one tenant unless explicitly marked as an approved "
                "shared research dataset'"
            )
        if self.time_range_end <= self.time_range_start:
            raise ValueError("time_range_end must be after time_range_start")
        return self
