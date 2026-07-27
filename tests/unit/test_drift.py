"""Schema-drift comparison tests (Phase 5)."""

from __future__ import annotations

from hermes_rpt.schemas.drift import DriftEventType, compare_snapshots
from hermes_rpt.schemas.introspection import (
    ColumnMetadata,
    SchemaIntrospectionResult,
    TableMetadata,
)


def _table(name: str, columns: list[tuple[str, str, bool]]) -> TableMetadata:
    return TableMetadata(
        schema_name="public",
        name=name,
        kind="table",
        columns=[
            ColumnMetadata(name=n, sql_type=t, is_nullable=nullable, ordinal_position=i + 1)
            for i, (n, t, nullable) in enumerate(columns)
        ],
    )


def _result(*tables: TableMetadata) -> SchemaIntrospectionResult:
    return SchemaIntrospectionResult(schemas=["public"], tables=list(tables))


def test_no_drift_between_identical_snapshots() -> None:
    table = _table("fleet_vehicle", [("id", "integer", False), ("plate", "text", False)])
    assert compare_snapshots(_result(table), _result(table.model_copy(deep=True))) == []


def test_detects_added_table() -> None:
    old = _result(_table("fleet_vehicle", [("id", "integer", False)]))
    new = _result(
        _table("fleet_vehicle", [("id", "integer", False)]),
        _table("fleet_driver", [("id", "integer", False)]),
    )
    events = compare_snapshots(old, new)
    assert any(
        e.event_type == DriftEventType.TABLE_ADDED and e.table == "public.fleet_driver"
        for e in events
    )


def test_detects_removed_table() -> None:
    old = _result(
        _table("fleet_vehicle", [("id", "integer", False)]),
        _table("legacy_table", [("x", "integer", False), ("y", "integer", False)]),
    )
    new = _result(_table("fleet_vehicle", [("id", "integer", False)]))
    events = compare_snapshots(old, new)
    assert any(
        e.event_type == DriftEventType.TABLE_REMOVED and e.table == "public.legacy_table"
        for e in events
    )


def test_detects_likely_rename_instead_of_remove_plus_add() -> None:
    columns = [
        ("id", "integer", False),
        ("plate", "text", False),
        ("model", "text", True),
        ("odometer", "numeric", True),
    ]
    old = _result(_table("fleet_vehicle", columns))
    new = _result(_table("vehicles", columns))  # same columns, new name

    events = compare_snapshots(old, new)
    event_types = {e.event_type for e in events}

    assert DriftEventType.TABLE_RENAMED_LIKELY in event_types
    # A likely rename must never also be reported as a plain add/remove — that would defeat the
    # point of flagging it distinctly, and "do not automatically approve inferred renames"
    # still holds: it's flagged, not merged into a single "no change" result.
    assert DriftEventType.TABLE_REMOVED not in event_types
    assert DriftEventType.TABLE_ADDED not in event_types


def test_dissimilar_tables_are_not_flagged_as_a_rename() -> None:
    old = _result(_table("fleet_vehicle", [("id", "integer", False), ("plate", "text", False)]))
    new = _result(
        _table("completely_unrelated", [("foo", "boolean", True), ("bar", "boolean", True)])
    )

    events = compare_snapshots(old, new)
    event_types = {e.event_type for e in events}
    assert DriftEventType.TABLE_RENAMED_LIKELY not in event_types
    assert DriftEventType.TABLE_REMOVED in event_types
    assert DriftEventType.TABLE_ADDED in event_types


def test_detects_added_and_removed_columns() -> None:
    old = _table("fleet_vehicle", [("id", "integer", False), ("plate", "text", False)])
    new = _table("fleet_vehicle", [("id", "integer", False), ("model", "text", True)])

    events = compare_snapshots(_result(old), _result(new))
    event_types_and_tables = {(e.event_type, e.table) for e in events}
    assert (DriftEventType.COLUMN_REMOVED, "public.fleet_vehicle") in event_types_and_tables
    assert (DriftEventType.COLUMN_ADDED, "public.fleet_vehicle") in event_types_and_tables


def test_detects_column_type_change() -> None:
    old = _table("fleet_vehicle", [("odometer", "integer", False)])
    new = _table("fleet_vehicle", [("odometer", "numeric", False)])
    events = compare_snapshots(_result(old), _result(new))
    assert any(e.event_type == DriftEventType.COLUMN_TYPE_CHANGED for e in events)


def test_detects_nullability_change() -> None:
    old = _table("fleet_vehicle", [("plate", "text", False)])
    new = _table("fleet_vehicle", [("plate", "text", True)])
    events = compare_snapshots(_result(old), _result(new))
    assert any(e.event_type == DriftEventType.COLUMN_NULLABILITY_CHANGED for e in events)


def test_detects_relationship_change() -> None:
    from hermes_rpt.schemas.introspection import ForeignKeyMetadata

    old = _table("fleet_vehicle", [("id", "integer", False), ("driver_id", "integer", True)])
    new = old.model_copy(deep=True)
    new.foreign_keys.append(
        ForeignKeyMetadata(
            constraint_name="fk_driver",
            columns=["driver_id"],
            referenced_schema="public",
            referenced_table="driver",
            referenced_columns=["id"],
        )
    )
    events = compare_snapshots(_result(old), _result(new))
    assert any(e.event_type == DriftEventType.RELATIONSHIP_CHANGED for e in events)
