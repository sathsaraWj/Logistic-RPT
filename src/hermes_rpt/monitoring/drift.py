"""Feature- and label-distribution drift (Phase 15) — compares two `FeatureStatistic`/
`LabelStatistics` snapshots (typically the two most recent dataset builds for a tenant) and
reports how much each shifted. Deliberately the same shape as `hermes_rpt.schemas.drift`'s
schema-drift comparison: a pure function over two already-computed summaries, no I/O, callers
decide what "significant" means for alerting.

Aggregate statistics only, same as `hermes_rpt.datasets.statistics` itself — no raw row value
ever passes through this module.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from hermes_rpt.datasets.statistics import FeatureStatistic, LabelStatistics

# A shift of one baseline standard deviation is the default "worth a look" threshold — coarse,
# deliberately not a statistical-significance test (that would need per-feature sample sizes and
# a real hypothesis test); this is a monitoring heuristic, not a research claim.
DEFAULT_SIGNIFICANCE_THRESHOLD = 1.0


class FeatureDriftResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    feature_name: str
    baseline_mean: float | None
    current_mean: float | None
    drift_score: float
    missing_rate_delta: float
    significant: bool


class LabelDriftResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_positive_fraction: float
    current_positive_fraction: float
    drift_score: float
    significant: bool


def _missing_rate(stat: FeatureStatistic) -> float:
    total = stat.count + stat.missing_count
    return stat.missing_count / total if total else 0.0


def compute_feature_drift(
    baseline: tuple[FeatureStatistic, ...],
    current: tuple[FeatureStatistic, ...],
    *,
    significance_threshold: float = DEFAULT_SIGNIFICANCE_THRESHOLD,
) -> tuple[FeatureDriftResult, ...]:
    """Only features present in *both* snapshots are compared — a feature that only exists in
    one build is a mapping/contract change, not a distribution drift, and is out of scope here
    (see `hermes_rpt.schemas.drift` for structural change detection)."""

    baseline_by_name = {s.feature_name: s for s in baseline}
    current_by_name = {s.feature_name: s for s in current}
    results = []
    for name in sorted(set(baseline_by_name) & set(current_by_name)):
        base, cur = baseline_by_name[name], current_by_name[name]
        if base.mean is None or cur.mean is None:
            # Non-numeric or entirely-missing in one snapshot — nothing to score a mean shift
            # against; the missing-rate delta below still carries signal on its own.
            drift_score = 0.0
        else:
            scale = base.std if base.std and base.std > 0 else max(abs(base.mean), 1.0)
            drift_score = abs(cur.mean - base.mean) / scale
        results.append(
            FeatureDriftResult(
                feature_name=name,
                baseline_mean=base.mean,
                current_mean=cur.mean,
                drift_score=drift_score,
                missing_rate_delta=_missing_rate(cur) - _missing_rate(base),
                significant=drift_score >= significance_threshold,
            )
        )
    return tuple(results)


def compute_label_drift(
    baseline: LabelStatistics,
    current: LabelStatistics,
    *,
    significance_threshold: float = 0.1,
) -> LabelDriftResult:
    drift_score = abs(current.positive_fraction - baseline.positive_fraction)
    return LabelDriftResult(
        baseline_positive_fraction=baseline.positive_fraction,
        current_positive_fraction=current.positive_fraction,
        drift_score=drift_score,
        significant=drift_score >= significance_threshold,
    )
