"""Tests for the feature normalizer (Phase 8)."""

from __future__ import annotations

from hermes_rpt.features.contract import (
    FeatureDataType,
    FeatureKind,
    FeatureSpec,
    LeakageRisk,
    MissingValueBehavior,
)
from hermes_rpt.features.normalizer import normalize


def _spec(
    data_type: FeatureDataType, missing: MissingValueBehavior, default: float | None = None
) -> FeatureSpec:
    return FeatureSpec(
        name="x",
        description="d",
        canonical_source="Trip.x",
        data_type=data_type,
        missing_value_behavior=missing,
        default_value=default,
        leakage_risk=LeakageRisk.NONE,
        availability_timestamp="now",
        kind=FeatureKind.DIRECT_FIELD,
        target_field="x",
    )


def test_missing_value_null_behavior_returns_none() -> None:
    spec = _spec(FeatureDataType.FLOAT, MissingValueBehavior.NULL)
    assert normalize(spec, None) is None


def test_missing_value_zero_behavior_returns_zero() -> None:
    spec = _spec(FeatureDataType.INTEGER, MissingValueBehavior.ZERO)
    assert normalize(spec, None) == 0


def test_missing_value_constant_behavior_returns_default() -> None:
    spec = _spec(FeatureDataType.FLOAT, MissingValueBehavior.CONSTANT, default=42.0)
    assert normalize(spec, None) == 42.0


def test_present_value_is_coerced_to_declared_type() -> None:
    assert normalize(_spec(FeatureDataType.INTEGER, MissingValueBehavior.NULL), "7") == 7
    assert normalize(_spec(FeatureDataType.FLOAT, MissingValueBehavior.NULL), "3.5") == 3.5
    assert normalize(_spec(FeatureDataType.BOOLEAN, MissingValueBehavior.NULL), 1) is True


def test_present_value_bypasses_missing_value_behavior() -> None:
    spec = _spec(FeatureDataType.FLOAT, MissingValueBehavior.CONSTANT, default=999.0)
    assert normalize(spec, 5.0) == 5.0
