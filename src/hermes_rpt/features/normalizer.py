"""Feature normalizer (Phase 8): coerces a raw extracted value to the `FeatureSpec`'s declared
`data_type` and applies its `missing_value_behavior` when the value is absent — "missing values
are explicitly represented" (the same principle as the ontology, applied to features)."""

from __future__ import annotations

from decimal import Decimal

from hermes_rpt.features.contract import FeatureDataType, FeatureSpec, MissingValueBehavior

RawFeatureValue = int | float | Decimal | str | bool | None


def normalize(feature: FeatureSpec, raw_value: RawFeatureValue) -> float | int | bool | None:
    if raw_value is None:
        if feature.missing_value_behavior == MissingValueBehavior.ZERO:
            return 0
        if feature.missing_value_behavior == MissingValueBehavior.CONSTANT:
            return feature.default_value
        return None

    if feature.data_type == FeatureDataType.INTEGER:
        return int(raw_value)
    if feature.data_type == FeatureDataType.FLOAT:
        return float(raw_value)
    if feature.data_type == FeatureDataType.BOOLEAN:
        return bool(raw_value)
    raise ValueError(f"Unsupported feature data type: {feature.data_type}")  # pragma: no cover
