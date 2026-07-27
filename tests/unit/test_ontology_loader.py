"""Tests for the bundled ontology YAML and the loader's validation rules (Phase 6)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_rpt.ontology.loader import OntologyValidationError, load_ontology_from_yaml
from hermes_rpt.ontology.registry import get_ontology
from hermes_rpt.ontology.schema import OntologyDefinition

_EXPECTED_ENTITIES_BY_DOMAIN = {
    "fleet": {
        "Vehicle",
        "VehicleType",
        "VehicleAssignment",
        "OdometerReading",
        "VehicleAvailability",
    },
    "workforce": {"Driver", "Operator", "Shift", "DriverAssignment", "SafetyEvent"},
    "operations": {"Trip", "Route", "RouteStop", "Delivery", "Order", "Package", "Customer"},
    "maintenance": {
        "MaintenanceEvent",
        "WorkOrder",
        "Fault",
        "SparePart",
        "InventoryTransaction",
        "Tyre",
        "TyreInstallation",
    },
    "cost": {"FuelEvent", "TripExpense", "MaintenanceCost", "TollEvent"},
}


def test_bundled_ontology_loads_and_has_all_expected_entities() -> None:
    ontology = get_ontology()
    assert ontology.version == "1"

    by_domain: dict[str, set[str]] = {}
    for entity in ontology.entities:
        by_domain.setdefault(entity.domain, set()).add(entity.name)

    assert by_domain == _EXPECTED_ENTITIES_BY_DOMAIN


def test_no_customer_table_names_appear_in_the_ontology_source() -> None:
    """A crude but effective guard for "do not hardcode customer table names into the
    ontology": none of the fixture-specific source names from Phase 4/5 should appear."""

    source = Path("configs/ontology/v1.yaml").read_text(encoding="utf-8").lower()
    for forbidden in (
        "fleet_vehicle",
        "registration_no",
        "assets",
        "total_km",
        "tenant_alpha",
        "tenant_beta",
    ):
        assert forbidden not in source


def _write(tmp_path: Path, content: dict) -> Path:  # type: ignore[type-arg]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return path


def test_relationship_to_unknown_entity_is_rejected(tmp_path: Path) -> None:
    broken = {
        "version": "test",
        "entities": [
            {
                "name": "Thing",
                "domain": "fleet",
                "fields": [
                    {
                        "name": "thing_id",
                        "logical_type": "string",
                        "required": True,
                        "is_business_identifier": True,
                    }
                ],
                "relationships": [
                    {
                        "name": "for_other",
                        "target_entity": "DoesNotExist",
                        "cardinality": "many_to_one",
                    }
                ],
            }
        ],
    }
    with pytest.raises(OntologyValidationError, match="unknown entity"):
        load_ontology_from_yaml(_write(tmp_path, broken))


def test_enum_field_without_values_is_rejected(tmp_path: Path) -> None:
    broken = {
        "version": "test",
        "entities": [
            {
                "name": "Thing",
                "domain": "fleet",
                "fields": [{"name": "status", "logical_type": "enum"}],
            }
        ],
    }
    with pytest.raises(OntologyValidationError):
        load_ontology_from_yaml(_write(tmp_path, broken))


def test_business_identifier_must_be_string(tmp_path: Path) -> None:
    broken = {
        "version": "test",
        "entities": [
            {
                "name": "Thing",
                "domain": "fleet",
                "fields": [
                    {"name": "thing_id", "logical_type": "integer", "is_business_identifier": True}
                ],
            }
        ],
    }
    with pytest.raises(OntologyValidationError, match="must be STRING"):
        load_ontology_from_yaml(_write(tmp_path, broken))


def test_unit_on_non_float_field_is_rejected(tmp_path: Path) -> None:
    broken = {
        "version": "test",
        "entities": [
            {
                "name": "Thing",
                "domain": "fleet",
                "fields": [{"name": "count", "logical_type": "integer", "unit": "kilometres"}],
            }
        ],
    }
    with pytest.raises(OntologyValidationError):
        load_ontology_from_yaml(_write(tmp_path, broken))


def test_duplicate_field_names_are_rejected(tmp_path: Path) -> None:
    broken = {
        "version": "test",
        "entities": [
            {
                "name": "Thing",
                "domain": "fleet",
                "fields": [
                    {"name": "x", "logical_type": "string"},
                    {"name": "x", "logical_type": "integer"},
                ],
            }
        ],
    }
    with pytest.raises(OntologyValidationError, match="duplicate field names"):
        load_ontology_from_yaml(_write(tmp_path, broken))


def test_duplicate_entity_names_are_rejected(tmp_path: Path) -> None:
    broken = {
        "version": "test",
        "entities": [
            {"name": "Thing", "domain": "fleet", "fields": []},
            {"name": "Thing", "domain": "cost", "fields": []},
        ],
    }
    with pytest.raises(OntologyValidationError, match="duplicate entity names"):
        load_ontology_from_yaml(_write(tmp_path, broken))


def test_invalid_yaml_is_reported_as_ontology_validation_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("entities: [this is not: valid: yaml", encoding="utf-8")
    with pytest.raises(OntologyValidationError):
        load_ontology_from_yaml(path)


def test_get_entity_raises_key_error_for_unknown_name() -> None:
    ontology: OntologyDefinition = get_ontology()
    with pytest.raises(KeyError):
        ontology.get_entity("NoSuchEntity")
