"""Tests for the delivery-delay-risk feature contract (Phase 8)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_rpt.features.contract import (
    DELIVERY_DELAY_RISK_CONTRACT,
    FeatureKind,
    FeatureSpec,
    LeakageRisk,
)


def test_contract_has_fourteen_features() -> None:
    assert len(DELIVERY_DELAY_RISK_CONTRACT.features) == 14


def test_contract_targets_trip() -> None:
    assert DELIVERY_DELAY_RISK_CONTRACT.target_entity == "Trip"


def test_every_feature_specifies_the_required_metadata() -> None:
    """Phase 8: "the task definition must specify" feature name, canonical source, data type,
    missing-value behaviour, leakage risk, availability timestamp, optional/required status —
    every field below is mandatory on `FeatureSpec` (Pydantic would already reject a missing
    one), this just makes the requirement explicit as a test."""

    for feature in DELIVERY_DELAY_RISK_CONTRACT.features:
        assert feature.name
        assert feature.canonical_source
        assert feature.data_type
        assert feature.missing_value_behavior
        assert feature.leakage_risk
        assert feature.availability_timestamp
        assert isinstance(feature.required, bool)


def test_only_direct_target_features_are_required() -> None:
    required = {f.name for f in DELIVERY_DELAY_RISK_CONTRACT.features if f.required}
    assert required == {"planned_departure_hour", "day_of_week", "planned_trip_distance_km"}


def test_mitigated_features_all_have_a_leakage_note() -> None:
    for feature in DELIVERY_DELAY_RISK_CONTRACT.features:
        if feature.leakage_risk == LeakageRisk.MITIGATED:
            assert feature.leakage_note, f"{feature.name} is MITIGATED but has no leakage_note"


def test_get_feature_returns_the_right_spec() -> None:
    feature = DELIVERY_DELAY_RISK_CONTRACT.get_feature("odometer_km")
    assert feature.kind == FeatureKind.RELATED_LATEST_VALUE


def test_get_feature_raises_for_unknown_name() -> None:
    with pytest.raises(KeyError):
        DELIVERY_DELAY_RISK_CONTRACT.get_feature("not_a_real_feature")


def test_direct_field_requires_target_field() -> None:
    with pytest.raises(ValidationError):
        FeatureSpec(
            name="x",
            description="d",
            canonical_source="Trip.x",
            data_type="float",
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="now",
            kind=FeatureKind.DIRECT_FIELD,
        )


def test_related_ratio_requires_filter_fields() -> None:
    with pytest.raises(ValidationError):
        FeatureSpec(
            name="x",
            description="d",
            canonical_source="Trip.x",
            data_type="float",
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="now",
            kind=FeatureKind.RELATED_RATIO,
            related_entity="Trip",
            related_fk_field_on_target="driver_id",
            related_timestamp_field="planned_departure_at",
        )


def test_related_recency_requires_timestamp_field() -> None:
    with pytest.raises(ValidationError):
        FeatureSpec(
            name="x",
            description="d",
            canonical_source="MaintenanceEvent.x",
            data_type="float",
            leakage_risk=LeakageRisk.NONE,
            availability_timestamp="now",
            kind=FeatureKind.RELATED_RECENCY_DAYS,
            related_entity="MaintenanceEvent",
            related_fk_field_on_target="vehicle_id",
        )


def test_related_count_does_not_require_a_timestamp_when_no_window() -> None:
    # Must not raise — number_of_stops in the real contract is exactly this shape.
    FeatureSpec(
        name="x",
        description="d",
        canonical_source="RouteStop.x",
        data_type="integer",
        leakage_risk=LeakageRisk.NONE,
        availability_timestamp="now",
        kind=FeatureKind.RELATED_COUNT,
        related_entity="RouteStop",
        related_fk_field_on_target="route_id",
    )
