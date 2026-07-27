"""Fingerprint determinism tests (Phase 5)."""

from __future__ import annotations

from hermes_rpt.schemas.fingerprint import compute_fingerprints, fingerprint_table
from hermes_rpt.schemas.introspection import (
    ColumnMetadata,
    ForeignKeyMetadata,
    SchemaIntrospectionResult,
    TableMetadata,
)


def _vehicle_table(column_order: list[str] | None = None) -> TableMetadata:
    columns = {
        "vehicle_id": ColumnMetadata(
            name="vehicle_id", sql_type="integer", is_nullable=False, ordinal_position=1
        ),
        "registration_no": ColumnMetadata(
            name="registration_no",
            sql_type="character varying",
            is_nullable=False,
            ordinal_position=2,
        ),
        "odometer_km": ColumnMetadata(
            name="odometer_km", sql_type="numeric", is_nullable=True, ordinal_position=3
        ),
    }
    order = column_order or list(columns)
    return TableMetadata(
        schema_name="public",
        name="fleet_vehicle",
        kind="table",
        columns=[columns[name] for name in order],
        primary_key_columns=["vehicle_id"],
    )


def test_table_fingerprint_is_deterministic_for_identical_input() -> None:
    assert fingerprint_table(_vehicle_table()) == fingerprint_table(_vehicle_table())


def test_table_fingerprint_is_independent_of_column_order() -> None:
    reordered = _vehicle_table(["odometer_km", "vehicle_id", "registration_no"])
    assert fingerprint_table(_vehicle_table()) == fingerprint_table(reordered)


def test_table_fingerprint_changes_when_a_column_type_changes() -> None:
    table = _vehicle_table()
    changed = table.model_copy(deep=True)
    changed.columns[2].sql_type = "double precision"
    assert fingerprint_table(table) != fingerprint_table(changed)


def test_table_fingerprint_changes_when_nullability_changes() -> None:
    table = _vehicle_table()
    changed = table.model_copy(deep=True)
    changed.columns[2].is_nullable = False
    assert fingerprint_table(table) != fingerprint_table(changed)


def test_table_fingerprint_is_sensitive_to_relationships() -> None:
    table = _vehicle_table()
    with_fk = table.model_copy(deep=True)
    with_fk.foreign_keys.append(
        ForeignKeyMetadata(
            constraint_name="fk_driver",
            columns=["driver_id"],
            referenced_schema="public",
            referenced_table="driver",
            referenced_columns=["driver_id"],
        )
    )
    assert fingerprint_table(table) != fingerprint_table(with_fk)


def test_schema_fingerprint_is_deterministic_regardless_of_table_order() -> None:
    table_a = _vehicle_table()
    table_b = table_a.model_copy(update={"name": "driver"}, deep=True)

    result_1 = SchemaIntrospectionResult(schemas=["public"], tables=[table_a, table_b])
    result_2 = SchemaIntrospectionResult(schemas=["public"], tables=[table_b, table_a])

    fp1, _ = compute_fingerprints(result_1)
    fp2, _ = compute_fingerprints(result_2)
    assert fp1 == fp2


def test_schema_fingerprint_changes_when_a_table_is_added() -> None:
    table_a = _vehicle_table()
    table_b = table_a.model_copy(update={"name": "driver"}, deep=True)

    fp_before, _ = compute_fingerprints(
        SchemaIntrospectionResult(schemas=["public"], tables=[table_a])
    )
    fp_after, _ = compute_fingerprints(
        SchemaIntrospectionResult(schemas=["public"], tables=[table_a, table_b])
    )
    assert fp_before != fp_after
