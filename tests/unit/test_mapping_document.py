"""Structural validation tests for MappingDocument and friends (Phase 7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_rpt.mappings.document import (
    FieldMapping,
    FilterCondition,
    JoinDefinition,
    MappingDocument,
    SourceTable,
    ValueSource,
)


def test_example_from_prompts_txt_parses_successfully() -> None:
    document = MappingDocument.model_validate(
        {
            "entity": "Vehicle",
            "source": {"schema": "public", "table": "fleet_vehicle"},
            "identity": {"vehicle_id": {"sources": [{"column": "vehicle_id"}]}},
            "fields": {
                "registration_number": {"sources": [{"column": "registration_no"}]},
                "current_odometer_km": {"sources": [{"column": "odometer_km"}]},
            },
        }
    )
    assert document.entity == "Vehicle"
    assert document.source.table == "fleet_vehicle"


def test_value_source_requires_exactly_one_kind() -> None:
    with pytest.raises(ValidationError):
        ValueSource()  # neither column, static, nor derived
    with pytest.raises(ValidationError):
        ValueSource(column="x", static="y")  # both


def test_value_source_rejects_non_identifier_column_names() -> None:
    with pytest.raises(ValidationError):
        ValueSource(column="drop table foo; --")


def test_source_unit_requires_a_column_source() -> None:
    with pytest.raises(ValidationError):
        ValueSource(static="5", source_unit="miles")


def test_field_mapping_requires_at_least_one_source() -> None:
    with pytest.raises(ValidationError):
        FieldMapping(sources=())


def test_filter_condition_requires_exactly_one_comparison() -> None:
    with pytest.raises(ValidationError):
        FilterCondition(column="status")  # none set
    with pytest.raises(ValidationError):
        FilterCondition(column="status", equals="x", not_equals="y")  # two set
    FilterCondition(column="status", is_null=True)  # valid


def test_join_definition_validates_identifiers() -> None:
    with pytest.raises(ValidationError):
        JoinDefinition(
            relationship="assigned_driver",
            schema="public",
            table="driver; DROP TABLE users",
            local_column="driver_id",
            foreign_column="driver_id",
        )


def test_source_table_rejects_non_identifier_names() -> None:
    with pytest.raises(ValidationError):
        SourceTable(schema="public", table="fleet vehicle")


def test_mapping_document_requires_at_least_one_identity_field() -> None:
    with pytest.raises(ValidationError):
        MappingDocument(
            entity="Vehicle",
            source=SourceTable(schema="public", table="fleet_vehicle"),
            identity={},
        )


def test_mapping_document_rejects_unknown_top_level_expression_kind() -> None:
    with pytest.raises(ValidationError):
        MappingDocument.model_validate(
            {
                "entity": "Vehicle",
                "source": {"schema": "public", "table": "fleet_vehicle"},
                "identity": {"vehicle_id": {"sources": [{"column": "vehicle_id"}]}},
                "fields": {
                    "registration_number": {
                        "sources": [{"derived": {"kind": "shell_exec", "cmd": "rm -rf /"}}]
                    }
                },
            }
        )
