"""Semantic validation of a `MappingDocument` — checks Pydantic structural validation
(`hermes_rpt.mappings.document`) can't express: does every referenced ontology field exist, is
every required field covered, do unit conversions make sense, do joins fulfil a real ontology
relationship, and (when a schema snapshot / connection allowlist is supplied) do referenced
columns/tables actually exist and remain on the approved allowlist.

This runs when a mapping moves from `draft` to `pending_validation` (Phase 7 lifecycle).
"""

from __future__ import annotations

from hermes_rpt.mappings.document import MappingDocument, ValueSource
from hermes_rpt.mappings.expressions import (
    ExpressionTooDeepError,
    check_expression_depth,
    referenced_columns,
)
from hermes_rpt.ontology.schema import OntologyEntityDefinition, OntologyFieldDefinition
from hermes_rpt.ontology.units import convert_to_canonical
from hermes_rpt.ontology.values import LogicalType


class MappingValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_mapping_document(
    document: MappingDocument,
    *,
    ontology_entity: OntologyEntityDefinition,
    available_columns: set[str] | None = None,
    schema_allowlist: list[str] | None = None,
    table_allowlist: list[str] | None = None,
) -> None:
    errors: list[str] = []

    if document.entity != ontology_entity.name:
        errors.append(
            f"Mapping targets entity {document.entity!r} but was validated against "
            f"{ontology_entity.name!r}"
        )

    field_defs = {f.name: f for f in ontology_entity.fields}
    relationship_defs = {r.name for r in ontology_entity.relationships}
    all_field_mappings = {**document.identity, **document.fields}

    for field_name, field_mapping in all_field_mappings.items():
        field_def = field_defs.get(field_name)
        if field_def is None:
            errors.append(f"Unknown ontology field: {field_name!r}")
            continue

        for source in field_mapping.sources:
            _validate_source_columns(source, available_columns, field_name, errors)
            _validate_source_unit(source, field_def, field_name, errors)
            _validate_value_map(source, field_def, field_name, errors)

    for field_def in ontology_entity.fields:
        covered = field_def.name in all_field_mappings
        has_fallback = covered and all_field_mappings[field_def.name].default is not None
        if field_def.required and not covered and not has_fallback:
            errors.append(f"Required ontology field {field_def.name!r} is not mapped")

    for join in document.joins:
        if join.relationship not in relationship_defs:
            errors.append(
                f"Join {join.relationship!r} does not match any relationship declared on "
                f"entity {ontology_entity.name!r} — joins are only permitted through approved "
                "relationships"
            )
        if schema_allowlist is not None and join.schema_name not in schema_allowlist:
            errors.append(f"Join schema {join.schema_name!r} is not on the connection's allowlist")
        if table_allowlist is not None and join.table not in table_allowlist:
            errors.append(f"Join table {join.table!r} is not on the connection's allowlist")

    if errors:
        raise MappingValidationError(errors)


def _validate_source_columns(
    source: ValueSource, available_columns: set[str] | None, field_name: str, errors: list[str]
) -> None:
    if source.derived is not None:
        # Checked unconditionally, before `available_columns is None` short-circuits below — a
        # Phase 16 security review found `referenced_columns`'s recursion (and `evaluate()`'s,
        # later, at extraction time) has no depth limit, so an over-nested expression is a risk
        # regardless of whether column-existence checking happens to run for this mapping.
        try:
            check_expression_depth(source.derived)
        except ExpressionTooDeepError as exc:
            errors.append(f"{field_name}: {exc}")
            return

    if available_columns is None:
        return
    if source.column is not None and source.column not in available_columns:
        errors.append(f"{field_name}: column {source.column!r} does not exist in the source table")
    if source.derived is not None:
        for column in referenced_columns(source.derived):
            if column not in available_columns:
                errors.append(
                    f"{field_name}: derived expression references unknown column {column!r}"
                )


def _validate_source_unit(
    source: ValueSource, field_def: OntologyFieldDefinition, field_name: str, errors: list[str]
) -> None:
    if source.source_unit is None:
        return
    if field_def.unit is None:
        errors.append(
            f"{field_name}: source_unit given but the ontology field has no canonical unit"
        )
        return
    try:
        convert_to_canonical(1.0, source_unit=source.source_unit, canonical_unit=field_def.unit)
    except ValueError as exc:
        errors.append(f"{field_name}: {exc}")


def _validate_value_map(
    source: ValueSource, field_def: OntologyFieldDefinition, field_name: str, errors: list[str]
) -> None:
    if source.value_map is None:
        return
    if field_def.logical_type != LogicalType.ENUM:
        errors.append(f"{field_name}: value_map is only valid for ENUM ontology fields")
        return
    allowed = set(field_def.enum_values or ())
    invalid_targets = set(source.value_map.values()) - allowed
    if invalid_targets:
        errors.append(
            f"{field_name}: value_map targets {sorted(invalid_targets)} are not valid values "
            f"for this enum (expected one of {sorted(allowed)})"
        )
