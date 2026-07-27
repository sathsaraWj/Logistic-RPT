"""Deterministic mapping-suggestion engine tests using Tenant Alpha's and Tenant Beta's
differently-named schemas (Phase 7 requirement: "add tests using Alpha and Beta's different
schemas") — same tables as docker/postgres-fixtures/{alpha,beta}/init.sql.
"""

from __future__ import annotations

from hermes_rpt.mappings.suggest import suggest_mapping
from hermes_rpt.ontology.registry import get_ontology
from hermes_rpt.schemas.introspection import ColumnMetadata, TableMetadata

_VEHICLE = get_ontology().get_entity("Vehicle")

_ALPHA_FLEET_VEHICLE = TableMetadata(
    schema_name="public",
    name="fleet_vehicle",
    kind="table",
    columns=[
        ColumnMetadata(
            name="vehicle_id", sql_type="integer", is_nullable=False, ordinal_position=1
        ),
        ColumnMetadata(
            name="registration_no",
            sql_type="character varying",
            is_nullable=False,
            ordinal_position=2,
        ),
        ColumnMetadata(
            name="model", sql_type="character varying", is_nullable=True, ordinal_position=3
        ),
        ColumnMetadata(
            name="odometer_km", sql_type="numeric", is_nullable=False, ordinal_position=4
        ),
    ],
    primary_key_columns=["vehicle_id"],
)

_BETA_ASSETS = TableMetadata(
    schema_name="public",
    name="assets",
    kind="table",
    columns=[
        ColumnMetadata(name="asset_id", sql_type="integer", is_nullable=False, ordinal_position=1),
        ColumnMetadata(
            name="tag", sql_type="character varying", is_nullable=False, ordinal_position=2
        ),
        ColumnMetadata(
            name="model_name", sql_type="character varying", is_nullable=True, ordinal_position=3
        ),
        ColumnMetadata(name="total_km", sql_type="numeric", is_nullable=False, ordinal_position=4),
    ],
    primary_key_columns=["asset_id"],
)


def test_alpha_vehicle_id_is_matched_via_primary_key_and_name() -> None:
    result = suggest_mapping(ontology_entity=_VEHICLE, table=_ALPHA_FLEET_VEHICLE)
    assert result.document.identity["vehicle_id"].sources[0].column == "vehicle_id"


def test_alpha_registration_number_matches_registration_no_despite_different_spelling() -> None:
    result = suggest_mapping(ontology_entity=_VEHICLE, table=_ALPHA_FLEET_VEHICLE)
    assert result.document.fields["registration_number"].sources[0].column == "registration_no"


def test_alpha_odometer_matches_odometer_km() -> None:
    result = suggest_mapping(ontology_entity=_VEHICLE, table=_ALPHA_FLEET_VEHICLE)
    assert result.document.fields["current_odometer_km"].sources[0].column == "odometer_km"


def test_beta_asset_id_is_matched_via_primary_key_despite_different_name() -> None:
    """Beta's PK is `asset_id`, not `vehicle_id` — the identifier still gets picked because it
    is the table's primary key, even though the name similarity to "vehicle_id" is weaker than
    Alpha's exact match. This is the concrete proof that the suggestion engine adapts to a
    genuinely different schema rather than only working on Alpha's naming."""

    result = suggest_mapping(ontology_entity=_VEHICLE, table=_BETA_ASSETS)
    assert result.document.identity["vehicle_id"].sources[0].column == "asset_id"


def test_beta_registration_number_matches_tag_or_is_left_unmapped() -> None:
    """ "tag" bears little name resemblance to "registration_number" — the engine should not
    force a low-confidence match; either it's absent, or it scored above the confidence floor
    honestly. Either outcome is acceptable; a *wrong* high-confidence match is not."""

    result = suggest_mapping(ontology_entity=_VEHICLE, table=_BETA_ASSETS)
    mapped = result.document.fields.get("registration_number")
    if mapped is not None:
        assert mapped.sources[0].column == "tag"


def test_beta_odometer_matches_total_km_via_type_and_partial_name() -> None:
    result = suggest_mapping(ontology_entity=_VEHICLE, table=_BETA_ASSETS)
    assert result.document.fields["current_odometer_km"].sources[0].column == "total_km"


def test_beta_model_name_matches_model_name_field() -> None:
    result = suggest_mapping(ontology_entity=_VEHICLE, table=_BETA_ASSETS)
    assert result.document.fields.get("model_name", None) is not None
    assert result.document.fields["model_name"].sources[0].column == "model_name"


def test_same_source_column_is_never_used_for_two_ontology_fields() -> None:
    result = suggest_mapping(ontology_entity=_VEHICLE, table=_ALPHA_FLEET_VEHICLE)
    all_columns = [
        source.column
        for mapping in {**result.document.identity, **result.document.fields}.values()
        for source in mapping.sources
        if source.column is not None
    ]
    assert len(all_columns) == len(set(all_columns))


def test_confidence_and_explanation_are_populated() -> None:
    result = suggest_mapping(ontology_entity=_VEHICLE, table=_ALPHA_FLEET_VEHICLE)
    assert 0.0 < result.confidence <= 1.0
    assert "fleet_vehicle" in result.explanation


def test_suggested_document_passes_structural_validation() -> None:
    """The suggestion engine's output must itself be a valid MappingDocument — it already is,
    by construction (suggest_mapping builds a real MappingDocument), but this pins that down
    as an explicit regression test."""

    from hermes_rpt.mappings.document import MappingDocument

    result = suggest_mapping(ontology_entity=_VEHICLE, table=_BETA_ASSETS)
    MappingDocument.model_validate(result.document.model_dump(mode="json", by_alias=True))
