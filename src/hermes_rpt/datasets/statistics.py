"""Feature and label statistics (Phase 9) — aggregate summaries only, safe to store in a
manifest. Never includes a raw per-row value.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict


class FeatureStatistic(BaseModel):
    model_config = ConfigDict(frozen=True)

    feature_name: str
    count: int
    missing_count: int
    mean: float | None = None
    std: float | None = None
    min: float | None = None
    max: float | None = None


class LabelStatistics(BaseModel):
    model_config = ConfigDict(frozen=True)

    count: int
    positive_count: int
    negative_count: int

    @property
    def positive_fraction(self) -> float:
        return self.positive_count / self.count if self.count else 0.0


def compute_feature_statistics(
    rows: list[dict[str, float | int | bool | None]], *, feature_names: tuple[str, ...]
) -> tuple[FeatureStatistic, ...]:
    stats: list[FeatureStatistic] = []
    for name in feature_names:
        values = [v for row in rows if (v := row.get(name)) is not None]
        missing = len(rows) - len(values)
        numeric = [float(v) for v in values]  # bool is an int subtype — coerces cleanly
        if numeric:
            mean = sum(numeric) / len(numeric)
            variance = sum((v - mean) ** 2 for v in numeric) / len(numeric)
            stats.append(
                FeatureStatistic(
                    feature_name=name,
                    count=len(numeric),
                    missing_count=missing,
                    mean=mean,
                    std=math.sqrt(variance),
                    min=min(numeric),
                    max=max(numeric),
                )
            )
        else:
            stats.append(FeatureStatistic(feature_name=name, count=0, missing_count=missing))
    return tuple(stats)


def compute_label_statistics(labels: list[int]) -> LabelStatistics:
    positive = sum(labels)
    return LabelStatistics(
        count=len(labels), positive_count=positive, negative_count=len(labels) - positive
    )
