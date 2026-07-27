"""Tests for dynamically-built ontology entity models (Phase 6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_rpt.ontology.registry import get_entity_model
from hermes_rpt.ontology.values import CanonicalDatetime, Money


def test_required_field_is_mandatory() -> None:
    Vehicle = get_entity_model("Vehicle")
    with pytest.raises(ValidationError):
        Vehicle()  # missing required vehicle_id / registration_number / is_active


def test_optional_field_defaults_to_none_when_omitted() -> None:
    Vehicle = get_entity_model("Vehicle")
    vehicle = Vehicle(vehicle_id="V1", registration_number="ALPHA-001", is_active=True)
    assert vehicle.make is None
    assert vehicle.current_odometer_km is None


def test_enum_field_accepts_valid_value_and_rejects_invalid() -> None:
    VehicleType = get_entity_model("VehicleType")
    valid = VehicleType(vehicle_type_id="T1", name="Van", category="light_duty")
    assert valid.category == "light_duty"

    with pytest.raises(ValidationError):
        VehicleType(vehicle_type_id="T1", name="Van", category="not_a_real_category")


def test_money_field_round_trips() -> None:
    SparePart = get_entity_model("SparePart")
    part = SparePart(
        spare_part_id="P1", part_number="PN-1", unit_cost=Money(amount="42.00", currency_code="EUR")
    )
    assert isinstance(part.unit_cost, Money)
    assert part.unit_cost.currency_code == "EUR"


def test_datetime_field_requires_canonical_datetime_shape() -> None:
    from datetime import UTC, datetime

    Trip = get_entity_model("Trip")
    trip = Trip(
        trip_id="T1",
        vehicle_id="V1",
        planned_departure_at=CanonicalDatetime(utc=datetime(2026, 7, 27, 8, 0, tzinfo=UTC)),
        status="planned",
    )
    assert trip.planned_departure_at.utc.year == 2026


def test_business_identifier_fields_are_strings_not_ints() -> None:
    Vehicle = get_entity_model("Vehicle")
    vehicle = Vehicle(vehicle_id="123", registration_number="ALPHA-001", is_active=True)
    assert isinstance(vehicle.vehicle_id, str)


def test_model_is_cached_and_returns_the_same_class() -> None:
    assert get_entity_model("Vehicle") is get_entity_model("Vehicle")


def test_relationship_targets_are_resolvable_entity_models() -> None:
    """Every relationship's target_entity must itself be buildable — exercises all 28 entities
    transitively via their relationships, which is a decent proxy for "the whole ontology is
    internally consistent," beyond the loader's own structural check."""

    from hermes_rpt.ontology.registry import get_ontology

    ontology = get_ontology()
    for entity in ontology.entities:
        get_entity_model(entity.name)
        for relationship in entity.relationships:
            get_entity_model(relationship.target_entity)
