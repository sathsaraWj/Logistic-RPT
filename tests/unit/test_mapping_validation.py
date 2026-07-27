"""Semantic mapping-validation tests (Phase 7)."""

from __future__ import annotations

import pytest

from hermes_rpt.mappings.document import FieldMapping, MappingDocument, SourceTable, ValueSource
from hermes_rpt.mappings.validation import MappingValidationError, validate_mapping_document
from hermes_rpt.ontology.registry import get_ontology

_VEHICLE = get_ontology().get_entity("Vehicle")


def _document(**field_overrides: FieldMapping) -> MappingDocument:
    return MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="public", table="fleet_vehicle"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="vehicle_id"),))},
        fields={
            "registration_number": FieldMapping(sources=(ValueSource(column="registration_no"),)),
            "is_active": FieldMapping(sources=(ValueSource(static=True),)),
            **field_overrides,
        },
    )


def test_valid_mapping_passes() -> None:
    validate_mapping_document(
        _document(),
        ontology_entity=_VEHICLE,
        available_columns={"vehicle_id", "registration_no"},
    )  # must not raise


def test_missing_required_field_is_rejected() -> None:
    document = MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="public", table="fleet_vehicle"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="vehicle_id"),))},
        fields={},  # registration_number and is_active (both required) are missing
    )
    with pytest.raises(MappingValidationError) as exc_info:
        validate_mapping_document(document, ontology_entity=_VEHICLE)
    assert any("registration_number" in e for e in exc_info.value.errors)
    assert any("is_active" in e for e in exc_info.value.errors)


def test_unknown_ontology_field_is_rejected() -> None:
    document = _document(this_field_does_not_exist=FieldMapping(sources=(ValueSource(column="x"),)))
    with pytest.raises(MappingValidationError, match="Unknown ontology field"):
        validate_mapping_document(document, ontology_entity=_VEHICLE)


def test_column_not_in_source_table_is_rejected() -> None:
    with pytest.raises(MappingValidationError, match="does not exist"):
        validate_mapping_document(
            _document(), ontology_entity=_VEHICLE, available_columns={"vehicle_id"}
        )


def test_unit_conversion_to_incompatible_unit_family_is_rejected() -> None:
    document = _document(
        current_odometer_km=FieldMapping(
            sources=(ValueSource(column="odometer", source_unit="pounds"),)
        )
    )
    with pytest.raises(MappingValidationError, match="not a valid unit"):
        validate_mapping_document(document, ontology_entity=_VEHICLE)


def test_unit_conversion_on_field_without_a_canonical_unit_is_rejected() -> None:
    document = _document(
        registration_number=FieldMapping(
            sources=(ValueSource(column="registration_no", source_unit="miles"),)
        )
    )
    with pytest.raises(MappingValidationError, match="no canonical unit"):
        validate_mapping_document(document, ontology_entity=_VEHICLE)


def test_valid_unit_conversion_passes() -> None:
    document = _document(
        current_odometer_km=FieldMapping(
            sources=(ValueSource(column="odometer_miles", source_unit="miles"),)
        )
    )
    validate_mapping_document(document, ontology_entity=_VEHICLE)  # must not raise


def test_value_map_on_non_enum_field_is_rejected() -> None:
    document = _document(
        registration_number=FieldMapping(
            sources=(ValueSource(column="registration_no", value_map={"a": "b"}),)
        )
    )
    with pytest.raises(MappingValidationError, match="only valid for ENUM"):
        validate_mapping_document(document, ontology_entity=_VEHICLE)


def test_value_map_targeting_invalid_enum_value_is_rejected() -> None:
    vehicle_type = get_ontology().get_entity("VehicleType")
    document = MappingDocument(
        entity="VehicleType",
        source=SourceTable(schema="public", table="vehicle_type"),
        identity={"vehicle_type_id": FieldMapping(sources=(ValueSource(column="id"),))},
        fields={
            "name": FieldMapping(sources=(ValueSource(column="name"),)),
            "category": FieldMapping(
                sources=(ValueSource(column="cat", value_map={"1": "not_a_real_category"}),)
            ),
        },
    )
    with pytest.raises(MappingValidationError, match="not valid values"):
        validate_mapping_document(document, ontology_entity=vehicle_type)


def test_join_referencing_unknown_relationship_is_rejected() -> None:
    from hermes_rpt.mappings.document import JoinDefinition

    document = MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="public", table="fleet_vehicle"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="vehicle_id"),))},
        fields={
            "registration_number": FieldMapping(sources=(ValueSource(column="registration_no"),)),
            "is_active": FieldMapping(sources=(ValueSource(static=True),)),
        },
        joins=(
            JoinDefinition(
                relationship="not_a_real_relationship",
                schema="public",
                table="driver",
                local_column="driver_id",
                foreign_column="driver_id",
            ),
        ),
    )
    with pytest.raises(MappingValidationError, match="does not match any relationship"):
        validate_mapping_document(document, ontology_entity=_VEHICLE)


def test_join_table_outside_allowlist_is_rejected() -> None:
    from hermes_rpt.mappings.document import JoinDefinition

    document = MappingDocument(
        entity="Vehicle",
        source=SourceTable(schema="public", table="fleet_vehicle"),
        identity={"vehicle_id": FieldMapping(sources=(ValueSource(column="vehicle_id"),))},
        fields={
            "registration_number": FieldMapping(sources=(ValueSource(column="registration_no"),)),
            "is_active": FieldMapping(sources=(ValueSource(static=True),)),
        },
        joins=(
            JoinDefinition(
                relationship="has_type",
                schema="public",
                table="vehicle_type",
                local_column="vehicle_type_id",
                foreign_column="vehicle_type_id",
            ),
        ),
    )
    with pytest.raises(MappingValidationError, match="not on the connection's allowlist"):
        validate_mapping_document(
            document,
            ontology_entity=_VEHICLE,
            schema_allowlist=["public"],
            table_allowlist=["fleet_vehicle"],  # vehicle_type deliberately not included
        )
